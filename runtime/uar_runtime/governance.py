"""Identity, RBAC, audit, quotas and budgets.

- Tenant comes only from the authenticated credential, never from request data.
- Sensitive actions record an audit intent first; if that write fails, the action does not run.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from fnmatch import fnmatchcase
from typing import Any

import jwt

from .config import Settings, TenantCfg, ToolPolicyCfg
from .errors import UARError, denied, redact
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


class Authenticator:
    def __init__(self, settings: Settings):
        self.s = settings
        self.tenants: dict[str, TenantCfg] = {t.id: t for t in settings.auth.tenants}
        self.keys = {k.id: (t, k) for t in settings.auth.tenants for k in t.api_keys}
        self._jwt_secret = None
        if settings.auth.jwt and settings.auth.jwt.hs256_secret_env:
            self._jwt_secret = os.environ.get(settings.auth.jwt.hs256_secret_env)

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
        m = _KEY_RE.fullmatch(key.strip())
        if not m or m.group(1) not in self.keys:
            raise UARError("unauthenticated", "invalid API key")
        tenant, kc = self.keys[m.group(1)]
        if not hmac.compare_digest(hash_key(key.strip()), kc.sha256):
            raise UARError("unauthenticated", "invalid API key")
        roles = tuple(kc.roles)
        return Principal(tenant.id, kc.subject, roles, kc.id, self.permissions(roles))

    def _from_jwt(self, token: str) -> Principal:
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

    def tenant(self, tid: str) -> TenantCfg:
        return self.tenants[tid]


# ---------------------------------------------------------------- audit


class Audit:
    def __init__(self, store: Store):
        self.store = store

    async def record(self, p: Principal, action: str, target: str, outcome: str, *, request_id: str = "",
                     run_id: str | None = None, details: dict | None = None, required: bool = True) -> None:
        """Write one audit row. With required=True a failure raises audit_unavailable (fail closed)."""
        try:
            await self.store.execute(
                "INSERT INTO audit (tenant, actor, action, target, outcome, request_id, run_id, policy_version, details)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                p.tenant, p.subject, action, target, outcome, request_id or None, run_id, POLICY_VERSION,
                jsonb(redact(details or {})))
        except Exception as e:
            log.error("audit write failed: %s", type(e).__name__)
            if required:
                raise UARError("audit_unavailable", "audit log unavailable; action not performed") from e


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
    """Evaluate tool policies in order. First matching rule decides; no match = deny (default deny)."""
    for i, rule in enumerate(policies):
        if not fnmatchcase(tool, rule.tool):
            continue
        if rule.tenants and p.tenant not in rule.tenants:
            continue
        if rule.roles and not set(rule.roles) & set(p.roles):
            continue
        if rule.effect == "deny":
            return False, f"denied by tool policy #{i}"
        for name, c in rule.args.items():
            v = args.get(name)
            if not isinstance(v, str):
                return False, f"argument {name} must be a string under policy #{i}"
            if c.prefix is not None and not any(v.replace("\\", "/").startswith(pre) for pre in c.prefix):
                return False, f"argument {name} outside allowed prefixes"
            if c.pattern is not None and not re.fullmatch(c.pattern, v):
                return False, f"argument {name} does not match policy pattern"
            if c.max_length is not None and len(v) > c.max_length:
                return False, f"argument {name} too long"
        return True, f"allowed by tool policy #{i}"
    return False, "no tool policy allows this call"


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


def tool_visible(policies: list[ToolPolicyCfg], p: Principal, tool: str) -> bool:
    """Catalog visibility: the first rule matching tool/tenant/role decides, ignoring argument constraints."""
    for rule in policies:
        if not fnmatchcase(tool, rule.tool):
            continue
        if rule.tenants and p.tenant not in rule.tenants:
            continue
        if rule.roles and not set(rule.roles) & set(p.roles):
            continue
        return rule.effect == "allow"
    return False
