"""Administration API (used by the Windows admin app): runtime info, inference history, API keys,
access policy and service logs."""
from __future__ import annotations

import logging

import httpx

from conftest import headers


def client(env, key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=env.http, headers=headers(key), timeout=30)


async def test_runtime_info(env):
    async with client(env, env.keys.admin) as c:
        info = (await c.get("/api/v1/admin/info")).json()
    assert info["version"] and info["tenant"] == "acme" and info["platform_admin"] is True
    assert "0004_api_keys" in info["migrations"] and info["worker"] is True
    kinds = {x["name"]: x for x in info["components"]}
    assert kinds["postgresql"]["ok"] and kinds["fake"]["kind"] == "provider:local" and kinds["fs"]["ok"]
    async with client(env, env.keys.dev) as c:
        assert (await c.get("/api/v1/admin/info")).status_code == 403


async def test_inference_history_is_tenant_scoped_and_paged(env):
    async with client(env, env.keys.dev) as c:
        for i in range(3):
            r = await c.post("/api/v1/inference", json={"model": "local:fake/echo", "input": f"history {i}"})
            assert r.status_code == 200
    async with client(env, env.keys.admin) as c:
        page = (await c.get("/api/v1/usage", params={"subject": "dev@acme", "limit": 2})).json()
        assert len(page["records"]) == 2 and int(page["next_before_id"]) > 0
        assert int(page["total_requests"]) >= 3 and int(page["total_input_tokens"]) > 0
        r0 = page["records"][0]
        assert r0["subject"] == "dev@acme" and r0["provider"] in ("fake", "fake_b") and r0["cost"]["currency"] == "USD"
        nxt = (await c.get("/api/v1/usage", params={"subject": "dev@acme", "limit": 2,
                                                    "before_id": page["next_before_id"]})).json()
        assert all(int(x["id"]) < int(page["next_before_id"]) for x in nxt["records"])
        assert (await c.get("/api/v1/usage", params={"model": "fake/echo"})).json()["records"]
        assert (await c.get("/api/v1/usage", params={"since": "not a date"})).status_code == 400
    async with client(env, env.keys.globex_admin) as c:
        assert all(x["subject"].endswith("@globex") for x in (await c.get("/api/v1/usage")).json()["records"])
    async with client(env, env.keys.dev) as c:
        assert (await c.get("/api/v1/usage")).status_code == 403


async def test_api_key_lifecycle(env):
    async with client(env, env.keys.admin) as c:
        r = await c.post("/api/v1/admin/keys", json={"subject": "reporting-bot@acme", "roles": ["viewer"],
                                                      "description": "read-only dashboards", "expires_in_days": 30})
        assert r.status_code == 200, r.text
        created = r.json()
        key, kid = created["api_key"], created["key"]["key_id"]
        assert key.startswith(f"uar_{kid}_") and created["key"]["status"] == "active" and created["key"]["expires_at"]
        listed = (await c.get("/api/v1/admin/keys")).json()["keys"]
        assert any(k["key_id"] == kid and k["source"] == "api" for k in listed)
        assert any(k["source"] == "config" and k["subject"] == "dev@acme" for k in listed)
        assert key not in r.text.replace(key, "", 1)   # the secret appears once, in api_key only
    row = await env.svc.store.fetchone("SELECT sha256 FROM api_keys WHERE key_id=%s", kid)
    assert row["sha256"] != key and len(row["sha256"]) == 64

    async with client(env, key) as bot:   # usable at once, with exactly the granted role
        assert (await bot.get("/api/v1/models")).status_code == 200
        assert (await bot.post("/api/v1/tool/execute", json={"tool": "fs.read_text",
                                                               "args": {"path": "docs/faq.md"}})).status_code == 403
        assert (await bot.post(f"/api/v1/admin/keys/{kid}/revoke", json={})).status_code == 403

    async with client(env, env.keys.globex_admin) as other:   # another tenant cannot see or revoke it
        assert all(k["key_id"] != kid for k in (await other.get("/api/v1/admin/keys")).json()["keys"])
        assert (await other.post(f"/api/v1/admin/keys/{kid}/revoke", json={})).status_code == 404

    async with client(env, env.keys.admin) as c:
        r = await c.post(f"/api/v1/admin/keys/{kid}/revoke", json={"reason": "rotated"})
        assert r.status_code == 200 and r.json()["status"] == "revoked"
        cfg_key = env.svc.auth.tenant("acme").api_keys[0].id
        assert (await c.post(f"/api/v1/admin/keys/{cfg_key}/revoke", json={})).status_code == 409
        assert all(k["key_id"] != kid for k in (await c.get("/api/v1/admin/keys")).json()["keys"])
        revoked = (await c.get("/api/v1/admin/keys", params={"include_revoked": "true"})).json()["keys"]
        assert next(k for k in revoked if k["key_id"] == kid)["status"] == "revoked"
    async with client(env, key) as bot:
        assert (await bot.get("/api/v1/models")).status_code == 401
    actions = await env.svc.store.fetchall("SELECT action FROM audit WHERE tenant='acme' AND target=%s", kid)
    assert {a["action"] for a in actions} == {"keys.create", "keys.revoke"}


async def test_api_key_validation_and_expiry(env):
    async with client(env, env.keys.admin) as c:
        assert (await c.post("/api/v1/admin/keys", json={"subject": "x", "roles": ["nope"]})).status_code == 400
        assert (await c.post("/api/v1/admin/keys", json={"subject": "", "roles": ["viewer"]})).status_code == 400
        assert (await c.post("/api/v1/admin/keys", json={"subject": "x", "roles": []})).status_code == 400
        r = await c.post("/api/v1/admin/keys", json={"subject": "short-lived", "roles": ["viewer"], "expires_in_days": 1})
        key, kid = r.json()["api_key"], r.json()["key"]["key_id"]
    async with client(env, env.keys.dev) as c:
        assert (await c.get("/api/v1/admin/keys")).status_code == 403
    await env.svc.store.execute("UPDATE api_keys SET expires_at = now() - interval '1 minute' WHERE key_id=%s", kid)
    await env.svc.auth.refresh_keys()
    async with client(env, key) as c:
        r = await c.get("/api/v1/models")
        assert r.status_code == 401 and "expired" in r.text


async def test_access_policy(env):
    async with client(env, env.keys.admin) as c:
        pol = (await c.get("/api/v1/admin/access")).json()
    roles = {r["name"]: r["permissions"] for r in pol["roles"]}
    assert pol["tenant"] == "acme" and "tools:execute" in roles["developer"] and "keys:manage" in pol["permissions"]


async def test_service_logs(env):
    logging.getLogger("uar.test").warning("admin log probe %s", 42)
    async with client(env, env.keys.admin) as c:
        page = (await c.get("/api/v1/admin/logs", params={"min_level": "WARNING", "limit": 2000})).json()
        probe = [x for x in page["records"] if x["message"] == "admin log probe 42"]
        assert probe and probe[0]["level"] == "WARNING" and probe[0]["logger"] == "uar.test"
        after = (await c.get("/api/v1/admin/logs", params={"after_seq": page["next_after_seq"]})).json()
        assert all(int(x["seq"]) > int(page["next_after_seq"]) for x in after["records"])
        env.svc.s.admin.platform_tenants = ["globex"]
        try:
            assert (await c.get("/api/v1/admin/logs")).status_code == 403   # acme is not a platform tenant
            info = (await c.get("/api/v1/admin/info")).json()
            assert info["platform_admin"] is False and info["components"] == []
        finally:
            env.svc.s.admin.platform_tenants = []
    async with client(env, env.keys.dev) as c:
        assert (await c.get("/api/v1/admin/logs")).status_code == 403
