"""Identity, RBAC, audit, quotas and budgets.

- Tenant comes only from the authenticated credential, never from request data.
- Sensitive actions record an audit intent first; if that write fails, the action does not run.
- Audit rows form a per-tenant hash chain (tamper evidence); the table is append-only in the database.
- Identity: API keys (service credentials), OIDC access tokens verified against the issuer's JWKS
  (IdP groups and service-account subjects map to roles), and HS256 tokens for development.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from fnmatch import fnmatchcase
from typing import Any

import httpx
import jwt

from .config import OidcCfg, Settings, TenantCfg, ToolPolicyCfg
from .errors import UARError, denied, redact
from .redaction import Redactor
from .store import Store, jsonb

log = logging.getLogger("uar.governance")
POLICY_VERSION = "2026-09-26.1"
_KEY_RE = re.compile(r"uar_([a-z0-9]{6,32})_([A-Za-z0-9_-]{16,128})")


@dataclass(frozen=True)
class Principal:
    tenant: str
    subject: str
    roles: tuple[str, ...]
    key_id: str = ""
    permissions: frozenset[str] = field(default_factory=frozenset)

    def has(self, perm: str) -> bool:
        return perm in self.permissions or "admin" in self.permissions

    def require(self, perm: str) -> None:
        if not self.has(perm):
            raise denied(f"missing permission {perm}", permission=perm)

    def snapshot(self) -> dict[str, Any]:
        return {"tenant": self.tenant, "subject": self.subject, "roles": list(self.roles), "key_id": self.key_id}


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class JwksCache:
    """Signing keys of one OIDC issuer. Fetched at startup, refreshed periodically and, rate-limited,
    when a token names an unknown key id (key rotation)."""

    MIN_REFRESH_S = 10.0

    def __init__(self, cfg: OidcCfg):
        self.cfg = cfg
        self.keys: dict[str, jwt.PyJWK] = {}
        self.last_error = ""
        self._fetched = 0.0
        self._pending: asyncio.Task | None = None

    async def refresh(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as c:
                r = await c.get(self.cfg.jwks_url)
                r.raise_for_status()
                doc = r.json()
            keys: dict[str, jwt.PyJWK] = {}
            for jwk in doc.get("keys", []):
                if jwk.get("use", "sig") != "sig":
                    continue
                try:
                    keys[str(jwk.get("kid", ""))] = jwt.PyJWK(jwk)
                except (jwt.PyJWKError, jwt.InvalidKeyError, KeyError, ValueError):
                    log.warning("oidc %s: skipped an unusable JWKS key", self.cfg.issuer)
            self.keys, self.last_error = keys, ""
        except Exception as e:  # keep serving with the keys we have
            self.last_error = type(e).__name__
            log.error("oidc %s: JWKS refresh failed: %s", self.cfg.issuer, self.last_error)
        finally:
            self._fetched = time.monotonic()

    def request_refresh(self) -> None:
        if self._pending and not self._pending.done():
            return
        if time.monotonic() - self._fetched < self.MIN_REFRESH_S:
            return
        try:
            self._pending = asyncio.get_running_loop().create_task(self.refresh())
        except RuntimeError:
            pass

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.jwks_refresh_s)
            await self.refresh()


class Authenticator:
    def __init__(self, settings: Settings):
        self.s = settings
        self.tenants: dict[str, TenantCfg] = {t.id: t for t in settings.auth.tenants}
        self.keys = {k.id: (t, k) for t in settings.auth.tenants for k in t.api_keys}
        self._jwt_secret = None
        if settings.auth.jwt and settings.auth.jwt.hs256_secret_env:
            self._jwt_secret = os.environ.get(settings.auth.jwt.hs256_secret_env)
        self.oidc = {o.issuer: JwksCache(o) for o in settings.auth.oidc}
        self._tasks: list[asyncio.Task] = []
        # Keys created through the admin API: key id -> row (tenant, sha256, subject, roles, expires_at).
        self.db_keys: dict[str, dict] = {}
        self.store: Store | None = None
        self.sts = None   # built-in token service (set by the service)

    async def start(self, store: Store | None = None) -> None:
        self.store = store
        await asyncio.gather(*(c.refresh() for c in self.oidc.values()))
        self._tasks = [asyncio.create_task(c.run()) for c in self.oidc.values()]
        if store is not None:
            await self.refresh_keys()
            self._tasks.append(asyncio.create_task(self._refresh_keys_forever()))

    async def refresh_keys(self) -> None:
        if self.store is None:
            return
        rows = await self.store.fetchall("SELECT key_id, tenant, sha256, subject, roles, expires_at FROM api_keys "
                                         "WHERE revoked_at IS NULL")
        self.db_keys = {r["key_id"]: r for r in rows if r["tenant"] in self.tenants}

    async def _refresh_keys_forever(self) -> None:
        while True:
            await asyncio.sleep(self.s.admin.key_refresh_s)
            try:
                await self.refresh_keys()
            except Exception as e:
                log.error("api key refresh failed: %s", type(e).__name__)

    def key_active(self, key_id: str) -> bool:
        if key_id.startswith("app:"):
            return self.sts is not None and self.sts.app_active(key_id[4:])
        if key_id in self.keys:
            return True
        row = self.db_keys.get(key_id)
        return row is not None and (row["expires_at"] is None or row["expires_at"] > datetime.now(timezone.utc))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def permissions(self, roles: tuple[str, ...]) -> frozenset[str]:
        perms: set[str] = set()
        for r in roles:
            perms.update(self.s.auth.roles.get(r, []))
        return frozenset(perms)

    def authenticate(self, api_key: str | None, bearer: str | None) -> Principal:
        if api_key:
            return self._from_key(api_key)
        if bearer:
            if bearer.startswith("uar_"):
                return self._from_key(bearer)
            return self._from_jwt(bearer)
        raise UARError("unauthenticated", "missing credentials (X-API-Key or Authorization: Bearer)")

    def _from_key(self, key: str) -> Principal:
        if self.sts is not None and self.s.sts.require_tokens:
            raise UARError("unauthenticated", "API keys are disabled on this runtime: register an application and use "
                           "an access token from POST /api/v1/oauth/token", details={"reason": "tokens_required"})
        m = _KEY_RE.fullmatch(key.strip())
        if m and m.group(1) in self.db_keys and m.group(1) not in self.keys:
            row = self.db_keys[m.group(1)]
            if not hmac.compare_digest(hash_key(key.strip()), row["sha256"]):
                raise UARError("unauthenticated", "invalid API key")
            if row["expires_at"] is not None and row["expires_at"] <= datetime.now(timezone.utc):
                raise UARError("unauthenticated", "API key has expired")
            roles = tuple(r for r in row["roles"] if r in self.s.auth.roles)
            return Principal(row["tenant"], row["subject"], roles, row["key_id"], self.permissions(roles))
        if not m or m.group(1) not in self.keys:
            raise UARError("unauthenticated", "invalid API key")
        tenant, kc = self.keys[m.group(1)]
        if not hmac.compare_digest(hash_key(key.strip()), kc.sha256):
            raise UARError("unauthenticated", "invalid API key")
        roles = tuple(kc.roles)
        return Principal(tenant.id, kc.subject, roles, kc.id, self.permissions(roles))

    def _from_jwt(self, token: str) -> Principal:
        if self.oidc or self.sts is not None:
            try:
                iss = jwt.decode(token, options={"verify_signature": False}).get("iss")
            except jwt.PyJWTError as e:
                raise UARError("unauthenticated", "invalid bearer token", details={"reason": type(e).__name__}) from e
            # The issuer only selects the verifier; verification pins it again.
            if self.sts is not None and self.sts.owns(iss):
                return self.sts.verify(token)
            if iss in self.oidc:
                return self._from_oidc(self.oidc[iss], token)
        cfg = self.s.auth.jwt
        if not cfg or not self._jwt_secret:
            raise UARError("unauthenticated", "bearer tokens are not enabled")
        try:
            claims = jwt.decode(token, self._jwt_secret, algorithms=["HS256"], audience=cfg.audience,
                                issuer=cfg.issuer, options={"require": ["exp", "sub", "iss", "aud"]})
        except jwt.PyJWTError as e:
            raise UARError("unauthenticated", "invalid bearer token", details={"reason": type(e).__name__}) from e
        tenant = claims.get(cfg.tenant_claim)
        if tenant not in self.tenants:
            raise UARError("unauthenticated", "token tenant is not configured")
        roles = tuple(r for r in claims.get(cfg.roles_claim, []) if r in self.s.auth.roles)
        return Principal(tenant, str(claims["sub"]), roles, "jwt", self.permissions(roles))

    def _from_oidc(self, cache: JwksCache, token: str) -> Principal:
        cfg = cache.cfg
        try:
            kid = str(jwt.get_unverified_header(token).get("kid", ""))
        except jwt.PyJWTError as e:
            raise UARError("unauthenticated", "invalid bearer token", details={"reason": type(e).__name__}) from e
        key = cache.keys.get(kid) or (next(iter(cache.keys.values())) if not kid and len(cache.keys) == 1 else None)
        if key is None:
            cache.request_refresh()
            raise UARError("unauthenticated", "token signing key is not known (yet); retry shortly",
                           details={"reason": "unknown_kid"})
        try:
            claims = jwt.decode(token, key.key, algorithms=list(cfg.algorithms), audience=cfg.audience,
                                issuer=cfg.issuer, leeway=cfg.leeway_s,
                                options={"require": ["exp", "sub", "iss", "aud"]})
        except jwt.PyJWTError as e:
            raise UARError("unauthenticated", "invalid bearer token", details={"reason": type(e).__name__}) from e
        tenant = cfg.tenant or cfg.tenant_map.get(str(claims.get(cfg.tenant_claim or "", "")))
        if tenant not in self.tenants:
            raise UARError("unauthenticated", "token tenant is not mapped to a configured tenant")
        groups = claims.get(cfg.groups_claim) or []
        groups = [groups] if isinstance(groups, str) else [str(g) for g in groups if isinstance(g, (str, int))]
        sub = str(claims["sub"])
        roles = {r for g in groups for r in cfg.group_roles.get(g, [])} | set(cfg.subject_roles.get(sub, []))
        roles_t = tuple(sorted(roles))
        return Principal(tenant, sub, roles_t, "oidc", self.permissions(roles_t))

    def tenant(self, tid: str) -> TenantCfg:
        return self.tenants[tid]


# ---------------------------------------------------------------- audit


GENESIS = "0" * 64
_CHAIN_FIELDS = ("tenant", "seq", "ts", "actor", "action", "target", "outcome", "request_id", "run_id",
                 "policy_version", "details")


def _ts(v: datetime) -> str:
    return v.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def chain_hash(prev_hash: str, row: dict) -> str:
    """hash = sha256(prev_hash + "\n" + canonical JSON of the row's chained fields)."""
    body = {k: (_ts(row[k]) if k == "ts" else row.get(k)) for k in _CHAIN_FIELDS}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(f"{prev_hash}\n{canon}".encode()).hexdigest()


class Audit:
    def __init__(self, store: Store, redactor: Redactor | None = None):
        self.store = store
        self.redactor = redactor

    async def record(self, p: Principal, action: str, target: str, outcome: str, *, request_id: str = "",
                     run_id: str | None = None, details: dict | None = None, required: bool = True) -> None:
        """Append one row to the tenant's audit chain. With required=True a failure raises
        audit_unavailable (fail closed)."""
        det = redact(details or {})
        if self.redactor is not None:
            det = self.redactor.for_audit(det)
        det = json.loads(json.dumps(det, default=str))   # exactly what jsonb will return
        row = {"tenant": p.tenant, "ts": datetime.now(timezone.utc), "actor": p.subject, "action": action,
               "target": target, "outcome": outcome, "request_id": request_id or None, "run_id": run_id,
               "policy_version": POLICY_VERSION, "details": det}
        try:
            await self.store.execute("INSERT INTO audit_heads (tenant, seq, hash) VALUES (%s, 0, %s) "
                                     "ON CONFLICT (tenant) DO NOTHING", p.tenant, GENESIS)
            async with self.store.tx() as c:
                head = await (await c.execute("SELECT seq, hash FROM audit_heads WHERE tenant=%s FOR UPDATE",
                                              (p.tenant,))).fetchone()
                row["seq"] = head["seq"] + 1
                h = chain_hash(head["hash"], row)
                await c.execute(
                    "INSERT INTO audit (tenant, ts, actor, action, target, outcome, request_id, run_id, policy_version,"
                    " details, seq, prev_hash, hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (p.tenant, row["ts"], p.subject, action, target, outcome, row["request_id"], run_id,
                     POLICY_VERSION, jsonb(det), row["seq"], head["hash"], h))
                await c.execute("UPDATE audit_heads SET seq=%s, hash=%s WHERE tenant=%s", (row["seq"], h, p.tenant))
        except Exception as e:
            log.error("audit write failed: %s", type(e).__name__)
            if required:
                raise UARError("audit_unavailable", "audit log unavailable; action not performed") from e

    async def verify(self, tenant: str, batch: int = 1000) -> dict:
        """Walk the tenant's chain: contiguous seq, prev_hash links, recomputed hashes, and the head."""
        anchor = await self.store.fetchone("SELECT seq, hash FROM audit_anchors WHERE tenant=%s", tenant)
        prev_seq, prev_hash = (anchor["seq"], anchor["hash"]) if anchor else (0, GENESIS)
        out = {"ok": True, "rows": 0, "first_seq": 0, "last_seq": prev_seq, "head_hash": prev_hash,
               "broken_at_seq": 0, "problem": "", "anchor_seq": anchor["seq"] if anchor else 0}
        unchained = await self.store.fetchone("SELECT count(*) AS n FROM audit WHERE tenant=%s AND seq IS NULL",
                                              tenant)
        out["unchained_rows"] = unchained["n"]

        def broken(seq: int, problem: str) -> dict:
            out.update(ok=False, broken_at_seq=seq, problem=problem)
            return out
        after = prev_seq
        while True:
            rows = await self.store.fetchall(
                "SELECT * FROM audit WHERE tenant=%s AND seq > %s ORDER BY seq LIMIT %s", tenant, after, batch)
            for r in rows:
                if r["seq"] != prev_seq + 1:
                    return broken(prev_seq + 1, f"row {prev_seq + 1} is missing")
                if r["prev_hash"] != prev_hash:
                    return broken(r["seq"], "prev_hash does not link to the previous row")
                if chain_hash(prev_hash, r) != r["hash"]:
                    return broken(r["seq"], "row content does not match its hash (modified)")
                if not out["first_seq"]:
                    out["first_seq"] = r["seq"]
                prev_seq, prev_hash = r["seq"], r["hash"]
                out["rows"] += 1
            if len(rows) < batch:
                break
            after = prev_seq
        out["last_seq"], out["head_hash"] = prev_seq, prev_hash
        head = await self.store.fetchone("SELECT seq, hash FROM audit_heads WHERE tenant=%s", tenant)
        if head and (head["seq"], head["hash"]) != (prev_seq, prev_hash):
            return broken(prev_seq + 1, f"chain ends at {prev_seq} but the head records {head['seq']} "
                                        "(rows removed from the end)")
        return out

    async def export(self, tenant: str, after_seq: int, limit: int) -> dict:
        limit = max(1, min(int(limit or 500), 1000))
        rows = await self.store.fetchall(
            "SELECT * FROM audit WHERE tenant=%s AND seq > %s ORDER BY seq LIMIT %s", tenant, int(after_seq), limit)
        head = await self.store.fetchone("SELECT hash FROM audit_heads WHERE tenant=%s", tenant)
        recs = [{"seq": r["seq"], "ts": _ts(r["ts"]), "actor": r["actor"], "action": r["action"],
                 "target": r["target"], "outcome": r["outcome"], "request_id": r["request_id"] or "",
                 "run_id": r["run_id"] or "", "policy_version": r["policy_version"] or "", "details": r["details"],
                 "prev_hash": r["prev_hash"], "hash": r["hash"]} for r in rows]
        return {"records": recs, "next_after_seq": rows[-1]["seq"] if rows else int(after_seq),
                "head_hash": head["hash"] if head else GENESIS}


# ---------------------------------------------------------------- quotas


class RateLimiter:
    """Per-principal request rate (token bucket) and concurrency. In-process: limits are per replica."""

    def __init__(self) -> None:
        self._buckets: dict[str, tuple[float, float]] = {}
        self._active: dict[str, int] = {}

    def acquire(self, key: str, per_minute: int, max_concurrent: int) -> None:
        now = time.monotonic()
        tokens, ts = self._buckets.get(key, (float(per_minute), now))
        tokens = min(float(per_minute), tokens + (now - ts) * per_minute / 60.0)
        if tokens < 1:
            raise UARError("rate_limited", "request rate limit exceeded", details={"retry_after_s": 60 / per_minute})
        if self._active.get(key, 0) >= max_concurrent:
            raise UARError("rate_limited", "too many concurrent requests")
        self._buckets[key] = (tokens - 1, now)
        self._active[key] = self._active.get(key, 0) + 1

    def release(self, key: str) -> None:
        self._active[key] = max(0, self._active.get(key, 1) - 1)


class Budget:
    """Atomic daily token reservation per tenant (settled with actual usage after the call)."""

    def __init__(self, store: Store):
        self.store = store

    async def reserve(self, tenant: TenantCfg, tokens: int) -> int:
        limit = tenant.quotas.tokens_per_day
        if limit is None:
            return 0
        if tokens > limit:
            raise UARError("budget_exceeded", "request exceeds the daily token budget")
        row = await self.store.fetchone(
            "INSERT INTO tenant_budget (tenant, day, reserved) VALUES (%s, %s, %s) "
            "ON CONFLICT (tenant, day) DO UPDATE SET reserved = tenant_budget.reserved + EXCLUDED.reserved "
            "WHERE tenant_budget.reserved + tenant_budget.used + EXCLUDED.reserved <= %s RETURNING reserved",
            tenant.id, date.today(), tokens, limit)
        if row is None:
            raise UARError("budget_exceeded", "daily token budget exhausted")
        return tokens

    async def settle(self, tenant: TenantCfg, reserved: int, used: int) -> None:
        if tenant.quotas.tokens_per_day is None:
            return
        await self.store.execute(
            "UPDATE tenant_budget SET reserved = GREATEST(0, reserved - %s), used = used + %s "
            "WHERE tenant = %s AND day = %s", reserved, used, tenant.id, date.today())


# ---------------------------------------------------------------- tool policy


def tool_decision(policies: list[ToolPolicyCfg], p: Principal, tool: str, args: dict[str, Any]) -> tuple[bool, str]:
    """Evaluate tool policies in order. First matching rule decides; no match = deny (default deny).
    A require_approval rule counts as allowed here; `tool_effect` tells the two apart."""
    effect, reason, _ = tool_effect(policies, p, tool, args)
    return effect != "deny", reason


def tool_effect(policies: list[ToolPolicyCfg], p: Principal, tool: str,
                args: dict[str, Any]) -> tuple[str, str, ToolPolicyCfg | None]:
    """First matching rule decides: ("allow" | "deny" | "require_approval", reason, rule)."""
    for i, rule in enumerate(policies):
        if not fnmatchcase(tool, rule.tool):
            continue
        if rule.tenants and p.tenant not in rule.tenants:
            continue
        if rule.roles and not set(rule.roles) & set(p.roles):
            continue
        if rule.effect == "deny":
            return "deny", f"denied by tool policy #{i}", rule
        for name, c in rule.args.items():
            v = args.get(name)
            if not isinstance(v, str):
                return "deny", f"argument {name} must be a string under policy #{i}", rule
            if c.prefix is not None and not any(v.replace("\\", "/").startswith(pre) for pre in c.prefix):
                return "deny", f"argument {name} outside allowed prefixes", rule
            if c.pattern is not None and not re.fullmatch(c.pattern, v):
                return "deny", f"argument {name} does not match policy pattern", rule
            if c.max_length is not None and len(v) > c.max_length:
                return "deny", f"argument {name} too long", rule
        if rule.effect == "require_approval":
            return "require_approval", f"requires approval under tool policy #{i}", rule
        return "allow", f"allowed by tool policy #{i}", rule
    return "deny", "no tool policy allows this call", None


def matches_any(value: str, patterns: list[str]) -> bool:
    return any(fnmatchcase(value, pat) for pat in patterns)


class Concurrency:
    """Named semaphores with a queue timeout (bounded provider concurrency)."""

    def __init__(self) -> None:
        self._sems: dict[str, asyncio.Semaphore] = {}

    def get(self, name: str, size: int) -> asyncio.Semaphore:
        if name not in self._sems:
            self._sems[name] = asyncio.Semaphore(size)
        return self._sems[name]


def tool_rule(policies: list[ToolPolicyCfg], p: Principal, tool: str) -> ToolPolicyCfg | None:
    """The first rule matching tool/tenant/role, ignoring argument constraints."""
    for rule in policies:
        if not fnmatchcase(tool, rule.tool):
            continue
        if rule.tenants and p.tenant not in rule.tenants:
            continue
        if rule.roles and not set(rule.roles) & set(p.roles):
            continue
        return rule
    return None


def tool_visible(policies: list[ToolPolicyCfg], p: Principal, tool: str) -> bool:
    """Catalog visibility: the first rule matching tool/tenant/role decides, ignoring argument constraints."""
    rule = tool_rule(policies, p, tool)
    return rule is not None and rule.effect in ("allow", "require_approval")
