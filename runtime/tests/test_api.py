"""M1/M2 gates over real HTTP, gRPC and WebSocket servers."""
from __future__ import annotations

import asyncio
import json
import time

import grpc
import httpx
import jwt
import pytest
import websockets

from conftest import headers
from uar_runtime.router.adapters.fake import FakeAdapter
from uarpb.v1 import runtime_pb2 as pb
from uarpb.v1 import runtime_pb2_grpc as pbg


async def sse_events(resp: httpx.Response) -> list[dict]:
    out = []
    async for line in resp.aiter_lines():
        if line.startswith("data:"):
            out.append(json.loads(line[5:]))
    return out


def grpc_md(key: str):
    return (("x-api-key", key),)


# ------------------------------------------------------------------ authentication & isolation

async def test_unauthenticated_rejected_on_all_transports(env):
    async with httpx.AsyncClient(base_url=env.http) as c:
        r = await c.get("/api/v1/models")
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthenticated"
        r = await c.get("/api/v1/models", headers={"X-API-Key": "***REMOVED***"})
        assert r.status_code == 401
        assert r.headers["x-request-id"]
    async with grpc.aio.insecure_channel(env.grpc) as ch:
        with pytest.raises(grpc.aio.AioRpcError) as ei:
            await pbg.RuntimeStub(ch).ListModels(pb.ListModelsRequest())
        assert ei.value.code() == grpc.StatusCode.UNAUTHENTICATED
        err = json.loads(dict(ei.value.trailing_metadata())["uar-error"])
        assert err["code"] == "unauthenticated" and err["request_id"]
    async with websockets.connect(env.http.replace("http", "ws") + "/api/v1/ws") as ws:
        await ws.send(json.dumps({"id": "1", "op": "list_models"}))
        assert json.loads(await ws.recv())["error"]["code"] == "unauthenticated"


async def test_rbac_viewer_cannot_infer_or_execute(env):
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.viewer)) as c:
        r = await c.post("/api/v1/inference", json={"model": "local:default", "input": "x"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "permission_denied"
        r = await c.post("/api/v1/tool/execute", json={"tool": "fs.read_text", "args": {"path": "docs/faq.md"}})
        assert r.status_code == 403
        assert (await c.get("/api/v1/models")).status_code == 200


async def test_cross_tenant_run_access_denied(env):
    async with httpx.AsyncClient(base_url=env.http) as c:
        r = await c.post("/api/v1/agent/run", headers=headers(env.keys.dev),
                         json={"agent_id": "in_app_assistant", "input": {"prompt": "hello"}})
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        other = headers(env.keys.other_tenant)
        assert (await c.get(f"/api/v1/runs/{run_id}", headers=other)).status_code == 404
        assert (await c.post(f"/api/v1/runs/{run_id}/cancel", headers=other, json={})).status_code == 404
        assert (await c.get(f"/api/v1/runs/{run_id}/events", headers=other)).status_code == 404
        # The other tenant also cannot see the agent (agents are tenant-scoped).
        r = await c.post("/api/v1/agent/run", headers=other, json={"agent_id": "does_not_exist", "input": {}})
        assert r.status_code == 404


async def test_jwt_bearer_and_tenant_binding(env):
    secret = __import__("os").environ["UAR_TEST_JWT_SECRET"]
    now = int(time.time())
    good = jwt.encode({"sub": "svc-a", "iss": "uar-dev", "aud": "uar", "exp": now + 60, "uar_tenant": "acme",
                       "uar_roles": ["developer"]}, secret, algorithm="HS256")
    bad_tenant = jwt.encode({"sub": "svc-a", "iss": "uar-dev", "aud": "uar", "exp": now + 60,
                             "uar_tenant": "not-configured", "uar_roles": ["admin"]}, secret, algorithm="HS256")
    async with httpx.AsyncClient(base_url=env.http) as c:
        r = await c.post("/api/v1/inference", json={"model": "local:default", "input": "jwt"},
                         headers={"Authorization": f"Bearer {good}"})
        assert r.status_code == 200 and r.json()["content"] == "echo: jwt"
        r = await c.get("/api/v1/models", headers={"Authorization": f"Bearer {bad_tenant}"})
        assert r.status_code == 401


async def test_request_limits(env):
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
        big = {"model": "local:default", "input": "x" * (env.svc.s.server.max_body_bytes + 10)}
        r = await c.post("/api/v1/inference", json=big)
        assert r.status_code == 413
        r = await c.post("/api/v1/inference", json={"model": "local:default", "input": "x", "unknown_field": 1})
        assert r.status_code == 400 and "unknown_field" in r.json()["error"]["message"]
        r = await c.post("/api/v1/inference", content=b"{not json", headers={"content-type": "application/json"})
        assert r.status_code == 400


async def test_rate_limit_returns_429(env):
    t = env.svc.auth.tenant("acme")
    old = t.quotas.requests_per_minute
    t.quotas.requests_per_minute = 2
    env.svc.limiter._buckets.clear()
    try:
        async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.ops)) as c:
            codes = [(await c.get("/api/v1/models")).status_code for _ in range(4)]
        assert codes[:2] == [200, 200] and 429 in codes[2:]
    finally:
        t.quotas.requests_per_minute = old
        env.svc.limiter._buckets.clear()


async def test_audit_failure_blocks_sensitive_action(env, monkeypatch):
    target = env.ws / "reports" / "audit-blocked.md"
    real = env.svc.audit.store.execute

    async def failing(sql, *args):
        if sql.startswith("INSERT INTO audit"):
            raise RuntimeError("audit store down")
        return await real(sql, *args)

    monkeypatch.setattr(env.svc.audit.store, "execute", failing)
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
        r = await c.post("/api/v1/tool/execute", json={"tool": "fs.write_text",
                                                       "args": {"path": "reports/audit-blocked.md", "content": "x"}})
    assert r.status_code == 503 and r.json()["error"]["code"] == "audit_unavailable"
    assert not target.exists(), "tool ran even though its audit intent could not be recorded"


async def test_audit_rows_written_without_payloads(env):
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
        r = await c.post("/api/v1/tool/execute",
                         json={"tool": "fs.read_text", "args": {"path": "docs/faq.md"}})
        assert r.status_code == 200
        rid = r.headers["x-request-id"]
    rows = await env.svc.store.fetchall("SELECT action, outcome, details FROM audit WHERE request_id=%s ORDER BY id", rid)
    assert [(r["action"], r["outcome"]) for r in rows] == [("tool.execute", "intent"), ("tool.execute", "completed")]
    assert "Returns within" not in json.dumps([r["details"] for r in rows])


# ------------------------------------------------------------------ inference over three transports

async def test_inference_identical_across_transports(env):
    body = {"model": "local:default", "input": "same answer"}
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
        http_resp = (await c.post("/api/v1/inference", json=body)).json()
    async with grpc.aio.insecure_channel(env.grpc) as ch:
        g = await pbg.RuntimeStub(ch).Infer(pb.InferenceRequest(model="local:default", input="same answer"),
                                            metadata=grpc_md(env.keys.dev))
    async with websockets.connect(env.http.replace("http", "ws") + "/api/v1/ws",
                                  additional_headers=headers(env.keys.dev)) as ws:
        await ws.send(json.dumps({"id": "a", "op": "infer", "body": body}))
        frames = []
        while True:
            f = json.loads(await ws.recv())
            frames.append(f)
            if f.get("done") or f.get("error"):
                break
    ws_text = "".join(f["event"]["token"]["text"] for f in frames if f.get("event", {}).get("type") == "token")
    assert http_resp["content"] == g.content == ws_text == "echo: same answer"
    assert http_resp["provider"] == g.provider == "fake"
    assert http_resp["usage"]["cost"] == {"amount": "0", "currency": "USD"}
    assert http_resp["route"]["reasons"][0] == "alias local:default -> fake/echo"


async def test_sse_stream_event_sequence(env):
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
        async with c.stream("POST", "/api/v1/inference",
                            json={"model": "local:fake/echo", "input": "stream me", "stream": True}) as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            evs = await sse_events(r)
    types = [e["type"] for e in evs]
    assert types[0] == "started" and types[-1] == "completed" and "usage" in types
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))
    assert "".join(e["token"]["text"] for e in evs if e["type"] == "token") == "echo: stream me"
    assert sum(1 for t in types if t in ("completed", "error")) == 1


async def test_grpc_stream(env):
    async with grpc.aio.insecure_channel(env.grpc) as ch:
        call = pbg.RuntimeStub(ch).InferStream(pb.InferenceRequest(model="local:default", input="g", stream=True),
                                               metadata=grpc_md(env.keys.dev))
        kinds = [ev.WhichOneof("body") async for ev in call]
    assert kinds[0] == "started" and kinds[-1] == "completed"


async def test_stream_disconnect_does_not_regenerate(env):
    before = FakeAdapter.calls
    body = {"model": "local:fake/echo", "input": "slow", "stream": True,
            "extensions": {"fake": {"chunks": ["a"] * 50, "delay_s": 0.05}}}
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
        async with c.stream("POST", "/api/v1/inference", json=body) as r:
            n = 0
            async for line in r.aiter_lines():
                if line.startswith("data:") and '"token"' in line:
                    n += 1
                    if n == 3:
                        break
    await asyncio.sleep(1.0)
    assert FakeAdapter.calls - before == 1, "an interrupted stream must not start a second generation"


# ------------------------------------------------------------------ routing, policy, budgets

async def test_policy_denials(env):
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
        r = await c.post("/api/v1/inference", json={"model": "cloud:default", "input": "x"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "permission_denied"
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.cloud_dev)) as c:
        r = await c.post("/api/v1/inference", json={"model": "cloud:default", "input": "x"})
        assert r.status_code == 200 and r.json()["provider"] == "fakecloud"
        assert r.json()["usage"]["cost"]["currency"] == "USD"
        r = await c.post("/api/v1/inference", json={"model": "cloud:default", "input": "x",
                                                    "data_class": "confidential"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "policy_denied"


async def test_capability_rejection_and_unknown_model(env):
    env.svc.s.provider("fake_b").capabilities = ["chat"]
    try:
        async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
            r = await c.post("/api/v1/inference", json={"model": "local:fake_b/echo", "input": "x",
                                                        "params": {"response_schema": {"type": "object"}}})
            assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_capability"
            r = await c.post("/api/v1/inference", json={"model": "local:no-such-model", "input": "x"})
            assert r.status_code == 404
    finally:
        env.svc.s.provider("fake_b").capabilities = ["chat", "stream", "tools", "json"]


async def test_fallback_on_provider_unavailable(env):
    body = {"model": "local:fake/echo", "input": "fb", "extensions": {"fake": {"fail": "unavailable"}}}
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
        r = await c.post("/api/v1/inference", json=body)
    assert r.status_code == 200, r.text
    route = r.json()["route"]
    assert route["provider"] == "fake_b" and route["fallback_used"] is True
    env.svc.router.breakers["fake"].success()


async def test_cloud_fallback_requires_tenant_opt_in(env):
    body = {"model": "local:fake/echo-large", "input": "x", "extensions": {"fake": {"fail": "unavailable"}}}
    async with httpx.AsyncClient(base_url=env.http) as c:
        r = await c.post("/api/v1/inference", json=body, headers=headers(env.keys.dev))
        assert r.status_code == 503, "acme has no cloud permission or fallback opt-in"
        env.svc.router.breakers["fake"].success()
        r = await c.post("/api/v1/inference", json=body, headers=headers(env.keys.cloud_dev))
        assert r.status_code == 200 and r.json()["route"]["model_class"] == "cloud"
    env.svc.router.breakers["fake"].success()


async def test_circuit_breaker_opens_and_recovers(env):
    br = env.svc.router.breakers["fake_b"]
    body = {"model": "local:fake_b/other", "input": "x", "extensions": {"fake_b": {"fail": "unavailable"}}}
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
        for _ in range(3):
            await c.post("/api/v1/inference", json=body)
        assert br.is_open()
        r = await c.post("/api/v1/inference", json={"model": "local:fake_b/other", "input": "x"})
        assert r.status_code == 503
        await asyncio.sleep(1.1)  # cooldown -> half-open trial succeeds -> closed
        r = await c.post("/api/v1/inference", json={"model": "local:fake_b/other", "input": "x"})
        assert r.status_code == 200 and not br.is_open()


async def test_daily_token_budget_is_atomic(env):
    t = env.svc.auth.tenant("cloudco")
    t.quotas.tokens_per_day = 3000
    try:
        body = {"model": "local:default", "input": "budget", "params": {"max_tokens": 1000}}
        async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.cloud_dev)) as c:
            codes = await asyncio.gather(*(c.post("/api/v1/inference", json=body) for _ in range(6)))
        statuses = sorted(r.status_code for r in codes)
        assert statuses.count(200) >= 2 and 429 in statuses
        row = await env.svc.store.fetchone("SELECT reserved, used FROM tenant_budget WHERE tenant='cloudco'")
        assert row["reserved"] == 0 and row["used"] <= 3000
    finally:
        t.quotas.tokens_per_day = 1_000_000


async def test_usage_ledger_and_metrics(env):
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
        r = await c.post("/api/v1/inference", json={"model": "local:default", "input": "ledger"})
        rid = r.headers["x-request-id"]
        m = (await c.get("/metrics")).text
    row = await env.svc.store.fetchone("SELECT provider, model, input_tokens, output_tokens, cost, estimated "
                                       "FROM usage_ledger WHERE request_id=%s", rid)
    assert row["provider"] == "fake" and row["cost"] == 0 and row["output_tokens"] > 0
    assert 'uar_model_tokens_total{direction="output",model="echo",provider="fake"}' in m


async def test_models_and_tools_catalog(env):
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
        models = (await c.get("/api/v1/models")).json()["models"]
        tools = (await c.get("/api/v1/tools")).json()["tools"]
    names = {m["name"] for m in models}
    assert "local:default" in names and "local:fake/echo" in names
    assert not any(m["model_class"] == "cloud" and not m["name"].startswith("cloud:default") for m in models)
    assert {t["name"] for t in tools} >= {"fs.read_text", "fs.write_text", "db.query"}
    assert next(t for t in tools if t["name"] == "fs.write_text")["side_effect"] == "write"


async def test_openapi_served_and_approvals_unimplemented(env):
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.admin)) as c:
        spec = (await c.get("/api/v1/openapi.json")).json()
        assert "/api/v1/inference" in spec["paths"]
        r = await c.post("/api/v1/approvals/ap_1/decision", json={"approve": True})
        assert r.status_code == 501


async def test_auto_tool_mode_executes_tools_under_policy(env):
    body = {"model": "local:fake/echo", "input": "please use tool", "tools": ["fs.read_text"], "tool_mode": "auto",
            "extensions": {"fake": {"tool_args": {"path": "docs/faq.md"}}}}
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
        r = await c.post("/api/v1/inference", json=body)
        assert r.status_code == 200, r.text
        assert r.json()["content"].startswith("tool said:") and "Returns within 30 days" in r.json()["content"]
        body["tool_mode"] = "suggest"
        r = await c.post("/api/v1/inference", json=body)
        assert r.json()["finish_reason"] == "tool_calls"
        assert r.json()["tool_calls"][0]["name"] == "fs.read_text"
