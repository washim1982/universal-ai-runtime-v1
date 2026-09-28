"""Administration API: runtime info, inference (usage) history, API keys, access policy, service logs.

Everything is scoped to the caller's tenant, except the process-wide views (service logs and runtime
components), which only administrators of the configured platform tenants may read.
"""
from __future__ import annotations

import os
import re
import secrets
import socket
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from .config import PERMISSIONS
from .errors import UARError, invalid
from .governance import Principal, hash_key
from .observability import LOG_BUFFER

if TYPE_CHECKING:
    from .service import RuntimeService

VERSION = "0.9.0"
_SUBJECT = re.compile(r"[A-Za-z0-9._@:+-]{1,128}")


def _iso(v: datetime | None) -> str:
    return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if v else ""


class AdminService:
    def __init__(self, svc: "RuntimeService"):
        self.svc = svc
        self.s = svc.s
        self.started_at = datetime.now(timezone.utc)

    def _platform(self, p: Principal) -> bool:
        tenants = self.s.admin.platform_tenants
        return p.has("admin") and (not tenants or p.tenant in tenants)

    # ------------------------------------------------------------ runtime info

    async def info(self, p: Principal) -> dict:
        p.require("admin")
        svc = self.svc
        out = {"version": VERSION, "profile": self.s.profile, "started_at": _iso(self.started_at),
               "uptime_s": int((datetime.now(timezone.utc) - self.started_at).total_seconds()),
               "worker": svc._worker is not None and not svc._worker.done(),
               "migrations": [r["version"] for r in await svc.store.fetchall(
                   "SELECT version FROM schema_migrations ORDER BY version")],
               "tenant": p.tenant, "subject": p.subject, "platform_admin": self._platform(p),
               "components": []}
        if not self._platform(p):
            return out
        out.update(host=socket.gethostname(), pid=os.getpid())
        comps = out["components"]
        db = await svc.store.ping()
        comps.append({"name": "postgresql", "kind": "database", "status": "reachable" if db else "unreachable",
                      "ok": db})
        opened = {pid for pid, b in svc.router.breakers.items() if time.monotonic() < b.open_until}
        for pc in self.s.providers:
            known = svc.router.catalog.get(pc.id)
            if pc.model_class not in self.s.egress.allowed_classes:
                st, ok = "egress disabled", False
            elif pc.model_class == "cloud" and pc.api_key_env and not os.environ.get(pc.api_key_env):
                st, ok = f"API key not set ({pc.api_key_env})" + (f"; {len(known)} models listed in configuration"
                                                                   if known else ""), False
            elif pc.id in opened:
                st, ok = "circuit open", False
            elif known is None:
                st, ok = "catalog unknown (not reachable at startup)", False
            else:
                st, ok = f"{len(known)} models", True
            comps.append({"name": pc.id, "kind": f"provider:{pc.model_class}", "status": st, "ok": ok})
        for sid, sess in svc.orch.sessions.items():
            comps.append({"name": sid, "kind": "mcp_server", "ok": bool(sess.tools),
                          "status": f"{len(sess.tools)} tools" if sess.tools else (sess.last_error or "not connected")[:200]})
        for pv in await svc.plugins.list(Principal(p.tenant, p.subject, p.roles, p.key_id, frozenset({"admin"}))):
            if pv.get("active"):
                comps.append({"name": f"{pv['plugin_id']}@{pv['version']}", "kind": "plugin",
                              "status": pv.get("status", ""), "ok": pv.get("status") == "active"})
        if self.s.sts.enabled:
            signing = svc.sts._signing_key()
            comps.append({"name": "token service (STS)", "kind": "sts", "ok": signing is not None,
                          "status": (f"signing with {signing[0]}" if signing else "no usable signing key")
                                    + (" · API keys disabled" if self.s.sts.require_tokens else "")})
        for iss, cache in svc.auth.oidc.items():
            comps.append({"name": iss, "kind": "oidc", "ok": bool(cache.keys) and not cache.last_error,
                          "status": cache.last_error or f"{len(cache.keys)} signing keys"})
        comps.append({"name": "worker", "kind": "worker", "ok": out["worker"],
                      "status": "running" if out["worker"] else "not running in this process"})
        return out

    # ------------------------------------------------------------ usage (inference history)

    async def usage(self, p: Principal, req: dict) -> dict:
        p.require("usage:read")
        limit = int(req.get("limit") or 100)
        if not 0 < limit <= 1000:
            raise invalid("limit must be between 1 and 1000")
        before = int(req.get("before_id") or 0)
        subject, model, since = req.get("subject") or "", req.get("model") or "", req.get("since") or ""
        since_ts = None
        if since:
            try:
                since_ts = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError:
                raise invalid("since must be an RFC 3339 timestamp") from None
        where = ["tenant=%s"]
        args: list = [p.tenant]
        if subject:
            where.append("subject=%s")
            args.append(subject)
        if model:
            where.append("(provider || '/' || model) ILIKE %s")
            args.append(f"%{model.replace('%', '').replace('_', '')}%")
        if since_ts:
            where.append("ts >= %s")
            args.append(since_ts)
        cond = " AND ".join(where)
        store = self.svc.store
        rows = await store.fetchall(f"SELECT * FROM usage_ledger WHERE {cond} AND (%s = 0 OR id < %s) "
                                    f"ORDER BY id DESC LIMIT %s", *args, before, before, limit)
        tot = await store.fetchone(f"SELECT count(*) AS n, COALESCE(sum(input_tokens),0) AS i, "
                                   f"COALESCE(sum(output_tokens),0) AS o, COALESCE(sum(cost),0) AS c "
                                   f"FROM usage_ledger WHERE {cond}", *args)
        recs = []
        for r in rows:
            rec = {"id": r["id"], "ts": _iso(r["ts"]), "subject": r["subject"], "request_id": r["request_id"] or "",
                   "run_id": r["run_id"] or "", "provider": r["provider"], "model": r["model"],
                   "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
                   "estimated": r["estimated"], "price_version": r["price_version"] or ""}
            if r["cost"] is not None:
                rec["cost"] = {"amount": str(Decimal(r["cost"]).normalize()), "currency": r["currency"] or ""}
            recs.append(rec)
        return {"records": recs, "next_before_id": rows[-1]["id"] if len(rows) == limit else 0,
                "total_requests": tot["n"], "total_input_tokens": tot["i"], "total_output_tokens": tot["o"],
                "total_cost": {"amount": str(Decimal(tot["c"]).normalize()), "currency": self.s.pricing.currency}}

    # ------------------------------------------------------------ API keys

    def _key_view(self, row: dict, source: str) -> dict:
        now = datetime.now(timezone.utc)
        status = ("revoked" if row.get("revoked_at") else
                  "expired" if row.get("expires_at") and row["expires_at"] <= now else "active")
        return {"key_id": row["key_id"], "subject": row["subject"], "roles": list(row["roles"]), "source": source,
                "description": row.get("description") or "", "created_by": row.get("created_by") or "",
                "created_at": _iso(row.get("created_at")), "expires_at": _iso(row.get("expires_at")),
                "revoked_at": _iso(row.get("revoked_at")), "status": status}

    async def list_keys(self, p: Principal, include_revoked: bool) -> dict:
        p.require("keys:manage")
        t = self.svc.auth.tenant(p.tenant)
        keys = [self._key_view({"key_id": k.id, "subject": k.subject, "roles": k.roles,
                                "description": "defined in the configuration file"}, "config") for k in t.api_keys]
        rows = await self.svc.store.fetchall(
            "SELECT * FROM api_keys WHERE tenant=%s AND (%s OR revoked_at IS NULL) ORDER BY created_at DESC",
            p.tenant, include_revoked)
        return {"keys": keys + [self._key_view(r, "api") for r in rows]}

    async def create_key(self, p: Principal, req: dict, request_id: str) -> dict:
        p.require("keys:manage")
        subject = (req.get("subject") or "").strip()
        roles = sorted(set(req.get("roles") or []))
        if not _SUBJECT.fullmatch(subject):
            raise invalid("subject is required (letters, digits and . _ @ : + -, up to 128 characters)")
        if not roles:
            raise invalid("at least one role is required")
        unknown = [r for r in roles if r not in self.s.auth.roles]
        if unknown:
            raise invalid(f"unknown roles {unknown}")
        granted = self.svc.auth.permissions(tuple(roles))
        if not p.has("admin") and not granted <= p.permissions:   # no privilege escalation
            raise UARError("permission_denied", "a key cannot be granted permissions its creator does not have")
        days = int(req.get("expires_in_days") or 0)
        if days < 0 or days > 3650:
            raise invalid("expires_in_days must be between 0 (no expiry) and 3650")
        kid = secrets.token_hex(6)
        while kid in self.svc.auth.keys:
            kid = secrets.token_hex(6)
        key = f"uar_{kid}_{secrets.token_urlsafe(32)}"
        expires = datetime.now(timezone.utc) + timedelta(days=days) if days else None
        await self.svc.audit.record(p, "keys.create", kid, "intent", request_id=request_id,
                                    details={"subject": subject, "roles": roles, "expires_in_days": days})
        row = await self.svc.store.fetchone(
            "INSERT INTO api_keys (key_id, tenant, sha256, subject, roles, description, created_by, expires_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *", kid, p.tenant, hash_key(key), subject, roles,
            (req.get("description") or "")[:500], p.subject, expires)
        await self.svc.auth.refresh_keys()
        return {"key": self._key_view(row, "api"), "api_key": key}

    async def revoke_key(self, p: Principal, key_id: str, reason: str, request_id: str) -> dict:
        p.require("keys:manage")
        if any(k.id == key_id for k in self.svc.auth.tenant(p.tenant).api_keys):
            raise UARError("failed_precondition", "this key is defined in the configuration file; remove it there")
        if key_id == p.key_id:
            raise UARError("failed_precondition", "a key cannot revoke itself")
        await self.svc.audit.record(p, "keys.revoke", key_id, "intent", request_id=request_id,
                                    details={"reason": reason[:500]})
        row = await self.svc.store.fetchone(
            "UPDATE api_keys SET revoked_at=COALESCE(revoked_at, now()), revoked_by=COALESCE(revoked_by, %s) "
            "WHERE key_id=%s AND tenant=%s RETURNING *", p.subject, key_id, p.tenant)
        if row is None:
            raise UARError("not_found", f"key {key_id} not found")
        self.svc.auth.db_keys.pop(key_id, None)   # effective at once here; other replicas within key_refresh_s
        return self._key_view(row, "api")

    async def access_policy(self, p: Principal) -> dict:
        p.require("keys:manage")
        oidc = []
        for o in self.s.auth.oidc:
            if o.tenant != p.tenant and p.tenant not in o.tenant_map.values():
                continue
            cache = self.svc.auth.oidc[o.issuer]
            maps = [{"source": g, "kind": "group", "roles": r} for g, r in o.group_roles.items()] + \
                   [{"source": sub, "kind": "subject", "roles": r} for sub, r in o.subject_roles.items()]
            oidc.append({"issuer": o.issuer, "audience": o.audience, "mappings": maps,
                         "keys": cache.last_error or f"{len(cache.keys)} signing keys"})
        return {"tenant": p.tenant, "permissions": sorted(PERMISSIONS), "oidc": oidc,
                "roles": [{"name": n, "permissions": sorted(ps)} for n, ps in sorted(self.s.auth.roles.items())]}

    # ------------------------------------------------------------ service logs

    async def logs(self, p: Principal, req: dict) -> dict:
        p.require("logs:read")
        if not self._platform(p):
            raise UARError("permission_denied", "service logs are process-wide; only administrators of the "
                           "platform tenants may read them (admin.platform_tenants)")
        limit = max(1, min(int(req.get("limit") or 500), 2000))
        recs = LOG_BUFFER.since(int(req.get("after_seq") or 0), limit, req.get("min_level") or "")
        return {"records": recs, "next_after_seq": recs[-1]["seq"] if recs else int(req.get("after_seq") or 0),
                "capacity": LOG_BUFFER.capacity}
