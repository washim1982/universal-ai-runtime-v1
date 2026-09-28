"""Built-in security token service (STS): application registration + OAuth 2.0 client credentials.

    1. An administrator registers an application (name, roles, token lifetime) and receives a
       client_id and a client_secret. The secret is shown once; only its SHA-256 is stored.
    2. The application exchanges its credentials for an access token:
           POST /api/v1/oauth/token   grant_type=client_credentials   (client_secret_basic or _post)
    3. It calls any endpoint with  Authorization: Bearer <access_token>.

Access tokens are ES256-signed JWTs (typ at+jwt) with a short lifetime. Every request re-checks the
application: disabling it, or removing a role from it, takes effect at once for tokens already
issued (on other replicas within admin.key_refresh_s). Revoking a secret stops new tokens; tokens
already issued with it stay valid until they expire (at most the application's token lifetime).

Signing keys live in PostgreSQL so every replica verifies the same tokens; with
sts.key_encryption_env set, private keys are encrypted at rest. The public keys are published at
/.well-known/jwks.json, so other services can verify UAR tokens too.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .config import Settings
from .errors import UARError, invalid
from .governance import Audit, Principal, hash_key
from .store import Store, jsonb

log = logging.getLogger("uar.sts")
TOKEN_PATH = "/api/v1/oauth/token"
JWKS_PATH = "/.well-known/jwks.json"
_KEY_LOCK = 0x5541_5233  # "UAR3"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")


class OAuthError(Exception):
    """An RFC 6749 token endpoint error: {"error": code, "error_description": text}."""

    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error, self.description, self.status = error, description, status


def _iso(v: datetime | None) -> str:
    return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if v else ""


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "app"


class Sts:
    def __init__(self, settings: Settings, store: Store, audit: Audit, permissions):
        self.s = settings
        self.cfg = settings.sts
        self.store = store
        self.audit = audit
        self.permissions = permissions            # roles -> permissions (Authenticator.permissions)
        self.apps: dict[str, dict] = {}           # client_id -> registration row (verification cache)
        self.keys: dict[str, dict] = {}           # kid -> {row, public, private}
        self._failures: dict[str, list[float]] = {}
        self._fetched = 0.0
        self._pending: asyncio.Task | None = None
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if not self.cfg.enabled:
            return
        await self._ensure_key()
        await self.refresh()
        self._task = asyncio.create_task(self._refresh_forever())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _refresh_forever(self) -> None:
        while True:
            await asyncio.sleep(self.s.admin.key_refresh_s)
            try:
                await self.refresh()
            except Exception as e:
                log.error("sts refresh failed: %s", type(e).__name__)

    async def refresh(self) -> None:
        rows = await self.store.fetchall("SELECT client_id, tenant, name, roles, token_ttl_s, disabled_at "
                                         "FROM app_registrations")
        self.apps = {r["client_id"]: r for r in rows}
        keys = await self.store.fetchall("SELECT * FROM sts_signing_keys ORDER BY active_from")
        loaded: dict[str, dict] = {}
        for k in keys:
            prev = self.keys.get(k["kid"])
            if prev is not None:
                prev["row"] = k
                loaded[k["kid"]] = prev
                continue
            try:
                private = serialization.load_pem_private_key(self._decrypt(k), password=None)
            except (ValueError, InvalidToken, UARError) as e:
                log.error("sts signing key %s cannot be loaded (%s); tokens it signed are still verified",
                          k["kid"], type(e).__name__)
                private = None
            loaded[k["kid"]] = {"row": k, "public": jwt.PyJWK(k["public_jwk"]).key, "private": private}
        self.keys = loaded
        self._fetched = time.monotonic()

    def _request_refresh(self) -> None:
        if (self._pending and not self._pending.done()) or time.monotonic() - self._fetched < 2:
            return
        try:
            self._pending = asyncio.get_running_loop().create_task(self.refresh())
        except RuntimeError:
            pass

    # ------------------------------------------------------------ signing keys

    def _fernet(self) -> Fernet | None:
        env = self.cfg.key_encryption_env
        if not env:
            return None
        secret = os.environ.get(env)
        if not secret:
            raise UARError("unavailable", f"sts.key_encryption_env: environment variable {env} is not set")
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))

    def _decrypt(self, row: dict) -> bytes:
        data = row["private_key"].encode()
        if not row["encrypted"]:
            return data
        f = self._fernet()
        if f is None:
            raise UARError("unavailable", "signing key is encrypted but sts.key_encryption_env is not set")
        return f.decrypt(data)

    async def _create_key(self, conn, active_from: datetime) -> str:
        kid = f"uar-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(4)}"
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        f = self._fernet()
        stored = f.encrypt(pem).decode() if f else pem.decode()
        jwk = jwt.algorithms.ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
        jwk.update(kid=kid, use="sig", alg="ES256")
        await conn.execute("INSERT INTO sts_signing_keys (kid, alg, private_key, encrypted, public_jwk, active_from) "
                           "VALUES (%s, 'ES256', %s, %s, %s, %s)", (kid, stored, f is not None, jsonb(jwk), active_from))
        return kid

    async def _ensure_key(self) -> None:
        """Create the first signing key (once, even with several replicas starting together)."""
        async with self.store.tx() as c:
            await c.execute("SELECT pg_advisory_xact_lock(%s)", (_KEY_LOCK,))
            row = await (await c.execute("SELECT count(*) AS n FROM sts_signing_keys WHERE retire_after IS NULL "
                                         "OR retire_after > now()")).fetchone()
            if row["n"] == 0:
                kid = await self._create_key(c, datetime.now(timezone.utc))
                log.info("created STS signing key %s", kid)

    def _signing_key(self) -> tuple[str, Any] | None:
        now = datetime.now(timezone.utc)
        usable = [(k["row"]["active_from"], kid, k["private"]) for kid, k in self.keys.items()
                  if k["private"] is not None and k["row"]["active_from"] <= now
                  and (k["row"]["retire_after"] is None or k["row"]["retire_after"] > now)]
        if not usable:
            return None
        _, kid, private = max(usable, key=lambda x: x[0])
        return kid, private

    def jwks(self) -> dict:
        now = datetime.now(timezone.utc)
        return {"keys": [k["row"]["public_jwk"] for k in self.keys.values()
                         if k["row"]["retire_after"] is None or k["row"]["retire_after"] > now]}

    def discovery(self, base_url: str) -> dict:
        base = base_url.rstrip("/")
        return {"issuer": self.cfg.issuer, "token_endpoint": base + TOKEN_PATH, "jwks_uri": base + JWKS_PATH,
                "grant_types_supported": ["client_credentials"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
                "id_token_signing_alg_values_supported": ["ES256"],
                "scopes_supported": sorted(self.s.auth.roles)}

    # ------------------------------------------------------------ token endpoint

    def _throttled(self, client_id: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self._failures.get(client_id, []) if now - t < 60]
        self._failures[client_id] = recent
        return len(recent) >= self.cfg.max_failed_attempts

    async def issue(self, client_id: str, client_secret: str, scope: str = "", grant_type: str = "") -> dict:
        if not self.cfg.enabled:
            raise OAuthError("unsupported_grant_type", "the token service is disabled", 400)
        if grant_type != "client_credentials":
            raise OAuthError("unsupported_grant_type", "grant_type must be client_credentials")
        if not client_id or not client_secret:
            raise OAuthError("invalid_client", "client_id and client_secret are required", 401)
        if self._throttled(client_id):
            raise OAuthError("invalid_client", "too many failed attempts for this client; retry in a minute", 429)
        app = await self.store.fetchone("SELECT * FROM app_registrations WHERE client_id=%s", client_id)
        ok = False
        if app is not None and app["disabled_at"] is None and app["tenant"] in {t.id for t in self.s.auth.tenants}:
            rows = await self.store.fetchall(
                "SELECT sha256 FROM app_secrets WHERE client_id=%s AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > now())", client_id)
            digest = hash_key(client_secret)
            ok = any(hmac.compare_digest(digest, r["sha256"]) for r in rows)
        if not ok:
            self._failures.setdefault(client_id, []).append(time.monotonic())
            if app is not None:
                await self.audit.record(Principal(app["tenant"], f"app:{client_id}", ()), "sts.token", client_id,
                                        "denied", details={"reason": "invalid client credentials"}, required=False)
            log.warning("sts: invalid client credentials", extra={"fields": {"client_id": client_id[:64]}})
            raise OAuthError("invalid_client", "client authentication failed", 401)
        granted = [r for r in app["roles"] if r in self.s.auth.roles]
        requested = scope.split() if scope.strip() else granted
        extra = sorted(set(requested) - set(granted))
        if extra:
            raise OAuthError("invalid_scope", f"the application is not granted {extra}")
        roles = sorted(set(requested))
        signing = self._signing_key()
        if signing is None:
            await self.refresh()
            signing = self._signing_key()
        if signing is None:
            raise OAuthError("temporarily_unavailable", "no usable signing key (check sts.key_encryption_env)", 503)
        kid, private = signing
        ttl = min(int(app["token_ttl_s"]), self.cfg.max_token_ttl_s)
        now = int(time.time())
        jti = secrets.token_urlsafe(16)
        claims = {"iss": self.cfg.issuer, "aud": self.cfg.audience, "sub": client_id, "client_id": client_id,
                  "uar_tenant": app["tenant"], "roles": roles, "scope": " ".join(roles), "iat": now, "nbf": now,
                  "exp": now + ttl, "jti": jti}
        token = jwt.encode(claims, private, algorithm="ES256", headers={"kid": kid, "typ": "at+jwt"})
        self._failures.pop(client_id, None)
        await self.store.execute("UPDATE app_registrations SET last_token_at=now() WHERE client_id=%s", client_id)
        await self.audit.record(Principal(app["tenant"], f"app:{client_id}", tuple(roles)), "sts.token", client_id,
                                "issued", details={"jti": jti, "roles": roles, "expires_in": ttl, "kid": kid})
        return {"access_token": token, "token_type": "Bearer", "expires_in": ttl, "scope": " ".join(roles)}

    # ------------------------------------------------------------ verification (every request)

    def owns(self, issuer: Any) -> bool:
        return self.cfg.enabled and issuer == self.cfg.issuer

    def verify(self, token: str) -> Principal:
        try:
            kid = str(jwt.get_unverified_header(token).get("kid", ""))
        except jwt.PyJWTError as e:
            raise UARError("unauthenticated", "invalid bearer token", details={"reason": type(e).__name__}) from e
        k = self.keys.get(kid)
        if k is None:
            self._request_refresh()
            raise UARError("unauthenticated", "token signing key is not known (yet); retry shortly",
                           details={"reason": "unknown_kid"})
        retire = k["row"]["retire_after"]
        if retire is not None and retire <= datetime.now(timezone.utc):
            raise UARError("unauthenticated", "token was signed with a retired key", details={"reason": "retired_key"})
        try:
            claims = jwt.decode(token, k["public"], algorithms=["ES256"], audience=self.cfg.audience,
                                issuer=self.cfg.issuer, leeway=30, options={"require": ["exp", "iat", "sub", "jti"]})
        except jwt.PyJWTError as e:
            raise UARError("unauthenticated", "invalid bearer token", details={"reason": type(e).__name__}) from e
        app = self.apps.get(claims["sub"])
        if app is None or app["disabled_at"] is not None or app["tenant"] != claims.get("uar_tenant"):
            raise UARError("unauthenticated", "the application is disabled or unknown", details={"reason": "app_disabled"})
        roles = tuple(sorted(set(claims.get("roles") or []) & set(app["roles"]) & set(self.s.auth.roles)))
        cid = claims["sub"]
        return Principal(app["tenant"], f"app:{cid}", roles, f"app:{cid}", self.permissions(roles))

    def app_active(self, client_id: str) -> bool:
        app = self.apps.get(client_id)
        return app is not None and app["disabled_at"] is None

    # ------------------------------------------------------------ administration

    def _secret_view(self, r: dict) -> dict:
        now = datetime.now(timezone.utc)
        status = "revoked" if r["revoked_at"] else "expired" if r["expires_at"] and r["expires_at"] <= now else "active"
        return {"secret_id": r["secret_id"], "hint": "…" + r["hint"], "created_at": _iso(r["created_at"]),
                "expires_at": _iso(r["expires_at"]), "status": status}

    def _app_view(self, a: dict, secret_rows: list[dict]) -> dict:
        return {"client_id": a["client_id"], "name": a["name"], "description": a["description"],
                "roles": list(a["roles"]), "token_ttl_s": a["token_ttl_s"],
                "status": "disabled" if a["disabled_at"] else "active", "created_by": a["created_by"],
                "created_at": _iso(a["created_at"]), "disabled_at": _iso(a["disabled_at"]),
                "last_token_at": _iso(a["last_token_at"]),
                "secrets": [self._secret_view(r) for r in secret_rows]}

    async def _get_app(self, p: Principal, client_id: str) -> dict:
        a = await self.store.fetchone("SELECT * FROM app_registrations WHERE client_id=%s AND tenant=%s",
                                      client_id, p.tenant)
        if a is None:
            raise UARError("not_found", f"application {client_id} not found")
        return a

    async def _secrets(self, client_id: str) -> list[dict]:
        return await self.store.fetchall("SELECT * FROM app_secrets WHERE client_id=%s ORDER BY created_at DESC",
                                         client_id)

    def info(self, p: Principal, base_url: str) -> dict:
        p.require("apps:manage")
        now = datetime.now(timezone.utc)
        signing = self._signing_key()
        keys = []
        for kid, k in sorted(self.keys.items(), key=lambda kv: kv[1]["row"]["active_from"], reverse=True):
            r = k["row"]
            status = ("retired" if r["retire_after"] and r["retire_after"] <= now else
                      "next" if r["active_from"] > now else
                      "signing" if signing and signing[0] == kid else "verify-only")
            keys.append({"kid": kid, "alg": r["alg"], "created_at": _iso(r["created_at"]),
                         "active_from": _iso(r["active_from"]), "retire_after": _iso(r["retire_after"]),
                         "status": status})
        base = base_url.rstrip("/")
        return {"enabled": self.cfg.enabled, "issuer": self.cfg.issuer, "audience": self.cfg.audience,
                "token_url": base + TOKEN_PATH, "jwks_url": base + JWKS_PATH,
                "require_tokens": self.cfg.require_tokens, "default_token_ttl_s": self.cfg.default_token_ttl_s,
                "max_token_ttl_s": self.cfg.max_token_ttl_s,
                "key_storage": "encrypted" if self.cfg.key_encryption_env else "unencrypted", "keys": keys}

    async def list_apps(self, p: Principal, include_disabled: bool) -> dict:
        p.require("apps:manage")
        rows = await self.store.fetchall(
            "SELECT * FROM app_registrations WHERE tenant=%s AND (%s OR disabled_at IS NULL) ORDER BY created_at DESC",
            p.tenant, include_disabled)
        secret_rows = await self.store.fetchall(
            "SELECT s.* FROM app_secrets s JOIN app_registrations a USING (client_id) WHERE a.tenant=%s "
            "ORDER BY s.created_at DESC", p.tenant)
        by_app: dict[str, list[dict]] = {}
        for r in secret_rows:
            by_app.setdefault(r["client_id"], []).append(r)
        return {"apps": [self._app_view(a, by_app.get(a["client_id"], [])) for a in rows]}

    def _check_roles(self, p: Principal, roles: list[str]) -> list[str]:
        roles = sorted(set(roles))
        if not roles:
            raise invalid("at least one role is required")
        unknown = [r for r in roles if r not in self.s.auth.roles]
        if unknown:
            raise invalid(f"unknown roles {unknown}")
        if not p.has("admin") and not self.permissions(tuple(roles)) <= p.permissions:
            raise UARError("permission_denied", "an application cannot be granted permissions its creator does not have")
        return roles

    async def _new_secret(self, conn, p: Principal, client_id: str, days: int) -> tuple[str, dict]:
        if days < 0 or days > 730:
            raise invalid("secret_expires_in_days must be between 0 (no expiry) and 730")
        secret = "uars_" + secrets.token_urlsafe(40)
        expires = datetime.now(timezone.utc) + timedelta(days=days) if days else None
        cur = await conn.execute(
            "INSERT INTO app_secrets (secret_id, client_id, sha256, hint, created_by, expires_at) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
            ("sec_" + secrets.token_hex(6), client_id, hash_key(secret), secret[-4:], p.subject, expires))
        return secret, await cur.fetchone()

    async def register_app(self, p: Principal, req: dict, request_id: str, base_url: str) -> dict:
        p.require("apps:manage")
        name = (req.get("name") or "").strip()
        if not _NAME.fullmatch(name):
            raise invalid("name is required: 1-64 letters, digits, spaces and . _ -")
        roles = self._check_roles(p, list(req.get("roles") or []))
        ttl = int(req.get("token_ttl_s") or self.cfg.default_token_ttl_s)
        if not 60 <= ttl <= self.cfg.max_token_ttl_s:
            raise invalid(f"token_ttl_s must be between 60 and {self.cfg.max_token_ttl_s}")
        days = int(req.get("secret_expires_in_days") if req.get("secret_expires_in_days") is not None else 365)
        client_id = f"{_slug(name)}-{secrets.token_hex(4)}"
        await self.audit.record(p, "apps.register", client_id, "intent", request_id=request_id,
                                details={"name": name, "roles": roles, "token_ttl_s": ttl})
        try:
            async with self.store.tx() as c:
                cur = await c.execute(
                    "INSERT INTO app_registrations (client_id, tenant, name, description, roles, token_ttl_s, created_by) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                    (client_id, p.tenant, name, (req.get("description") or "")[:500], roles, ttl, p.subject))
                app = await cur.fetchone()
                secret, srow = await self._new_secret(c, p, client_id, days)
        except Exception as e:
            if "app_registrations_name" in str(e):
                raise UARError("conflict", f"an active application named {name!r} already exists") from None
            raise
        self.apps[client_id] = app
        return {"app": self._app_view(app, [srow]), "client_id": client_id, "client_secret": secret,
                "token_url": base_url.rstrip("/") + TOKEN_PATH}

    async def create_secret(self, p: Principal, client_id: str, days: int, request_id: str, base_url: str) -> dict:
        p.require("apps:manage")
        app = await self._get_app(p, client_id)
        if app["disabled_at"]:
            raise UARError("failed_precondition", "the application is disabled")
        active = [r for r in await self._secrets(client_id) if self._secret_view(r)["status"] == "active"]
        if len(active) >= 2:
            raise UARError("failed_precondition", "an application can have two active secrets; revoke one first")
        await self.audit.record(p, "apps.secret.create", client_id, "intent", request_id=request_id,
                                details={"expires_in_days": days})
        async with self.store.tx() as c:
            secret, _ = await self._new_secret(c, p, client_id, days)
        return {"app": self._app_view(app, await self._secrets(client_id)), "client_id": client_id,
                "client_secret": secret, "token_url": base_url.rstrip("/") + TOKEN_PATH}

    async def revoke_secret(self, p: Principal, client_id: str, secret_id: str, request_id: str) -> dict:
        p.require("apps:manage")
        app = await self._get_app(p, client_id)
        await self.audit.record(p, "apps.secret.revoke", client_id, "intent", request_id=request_id,
                                details={"secret_id": secret_id})
        n = await self.store.execute("UPDATE app_secrets SET revoked_at=COALESCE(revoked_at, now()) "
                                     "WHERE secret_id=%s AND client_id=%s", secret_id, client_id)
        if n == 0:
            raise UARError("not_found", f"secret {secret_id} not found")
        return self._app_view(app, await self._secrets(client_id))

    async def disable_app(self, p: Principal, client_id: str, reason: str, request_id: str) -> dict:
        p.require("apps:manage")
        if p.key_id == f"app:{client_id}":
            raise UARError("failed_precondition", "an application cannot disable itself")
        await self._get_app(p, client_id)
        await self.audit.record(p, "apps.disable", client_id, "intent", request_id=request_id,
                                details={"reason": reason[:500]})
        app = await self.store.fetchone(
            "UPDATE app_registrations SET disabled_at=COALESCE(disabled_at, now()), disabled_by=COALESCE(disabled_by, %s) "
            "WHERE client_id=%s AND tenant=%s RETURNING *", p.subject, client_id, p.tenant)
        self.apps[client_id] = app          # tokens already issued stop working here at once
        return self._app_view(app, await self._secrets(client_id))

    async def rotate_key(self, p: Principal, request_id: str, base_url: str, platform: bool) -> dict:
        p.require("admin")
        if not platform:
            raise UARError("permission_denied", "signing keys are shared by all tenants; only platform administrators "
                           "may rotate them (admin.platform_tenants)")
        now = datetime.now(timezone.utc)
        active_from = now + timedelta(seconds=self.cfg.key_activation_delay_s)
        retire = active_from + timedelta(seconds=self.cfg.max_token_ttl_s + 60)
        await self.audit.record(p, "sts.key.rotate", "signing-key", "intent", request_id=request_id)
        async with self.store.tx() as c:
            await c.execute("SELECT pg_advisory_xact_lock(%s)", (_KEY_LOCK,))
            await c.execute("UPDATE sts_signing_keys SET retire_after=%s WHERE retire_after IS NULL", (retire,))
            kid = await self._create_key(c, active_from)
        log.info("rotated STS signing key: %s signs from %s", kid, active_from.isoformat())
        await self.refresh()
        return self.info(p, base_url)
