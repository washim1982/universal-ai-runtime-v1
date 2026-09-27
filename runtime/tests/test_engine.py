"""M4 gates: durable agent engine, recovery, fencing, limits and sub-agent permissions."""
from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest
import yaml

from conftest import ROOT, headers
from uar_runtime.engine.executor import LeaseLost
from uar_runtime.errors import UARError
from uar_runtime.governance import Principal


def agent(aid: str, nodes: list, edges: list, *, start=None, perms=None, limits=None, version="1.0.0", **spec):
    return {"apiVersion": "uar/v1", "kind": "Agent", "metadata": {"id": aid, "version": version},
            "spec": {"start": start or nodes[0]["id"], "nodes": nodes, "edges": edges,
                     "permissions": perms or {"models": ["local:*"], "tools": [], "agents": []},
                     **({"limits": limits} if limits else {}), **spec}}


def dev(env) -> Principal:
    return env.svc.auth.authenticate(env.keys.dev, None)


def ops(env) -> Principal:
    return env.svc.auth.authenticate(env.keys.ops, None)


async def register(env, definition):
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
        return await c.post("/api/v1/agents", json={"definition": definition})


async def run_to_end(env, agent_id, input_, key=None, timeout=30):
    async with httpx.AsyncClient(base_url=env.http, headers=headers(key or env.keys.dev), timeout=60) as c:
        r = await c.post("/api/v1/agent/run", json={"agent_id": agent_id, "input": input_})
        assert r.status_code == 202, r.text
        rid = r.json()["run_id"]
        for _ in range(int(timeout / 0.1)):
            run = (await c.get(f"/api/v1/runs/{rid}")).json()
            if run["status"] in ("succeeded", "failed", "cancelled", "needs_attention"):
                return run
            await asyncio.sleep(0.1)
    raise AssertionError(f"run {rid} did not finish: {run}")


# ------------------------------------------------------------------ graph validation

@pytest.mark.parametrize("mutate, message", [
    (lambda d: (d["spec"]["nodes"].append({"id": "c", "type": "transform", "value": 3}),
                d["spec"]["edges"].extend([{"from": "a", "to": "c"}, {"from": "c", "to": "a"}])),
     "cycle without a loop node"),
    (lambda d: d["spec"]["nodes"].append({"id": "orphan", "type": "return"}), "unreachable nodes: orphan"),
    (lambda d: d["spec"]["edges"].append({"from": "a", "to": "missing"}), "unknown node missing"),
    (lambda d: d["spec"]["nodes"][0].update(value="${ nodes.nope.output }"), "unknown node"),
    (lambda d: d["spec"]["nodes"][0].update(value="${ 1 + }"), "invalid expression"),
    (lambda d: d["spec"]["nodes"].__setitem__(0, {"id": "a", "type": "tool", "tool": "fs.write_text"}),
     "not in permissions.tools"),
    (lambda d: d["spec"].update(limits={"max_steps": 100000}), "max_steps"),
    (lambda d: d["spec"]["nodes"].__setitem__(0, {"id": "a", "type": "approval"}), "approval nodes"),
    (lambda d: d["spec"]["nodes"][1].pop("type"), "type"),
])
async def test_invalid_graphs_rejected(env, mutate, message):
    d = agent("bad_graph", [{"id": "a", "type": "transform", "value": 1}, {"id": "b", "type": "return", "value": 2}],
              [{"from": "a", "to": "b"}])
    mutate(d)
    r = await register(env, d)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_graph", r.text
    assert message in json.dumps(r.json()["error"]), r.json()


async def test_versions_are_immutable(env):
    d = agent("immut", [{"id": "a", "type": "return", "value": "v1"}], [])
    assert (await register(env, d)).status_code == 200
    assert (await register(env, d)).status_code == 200  # identical re-registration is idempotent
    d["spec"]["nodes"][0]["value"] = "changed"
    r = await register(env, d)
    assert r.status_code == 409


async def test_yaml_registration(env):
    body = (ROOT / "examples" / "agents" / "in_app_assistant.yaml").read_bytes()         .replace(b"id: in_app_assistant", b"id: yaml_assistant").replace(b"1.0.0", b"1.0.1")
    async with httpx.AsyncClient(base_url=env.http, headers={**headers(env.keys.dev),
                                                             "content-type": "application/yaml"}) as c:
        r = await c.post("/api/v1/agents", content=body)
    assert r.status_code == 200 and r.json()["version"] == "1.0.1" and r.json()["digest"].startswith("sha256:")


# ------------------------------------------------------------------ execution

async def test_report_generator_end_to_end(env):
    run = await run_to_end(env, "report_generator", {"report_name": "q2_summary"})
    assert run["status"] == "succeeded", run
    assert run["output"] == {"path": "reports/q2_summary.md", "title": "fake"}
    text = (env.ws / "reports" / "q2_summary.md").read_text(encoding="utf-8")
    assert text.startswith("# fake")
    assert run["usage"]["cost"] == {"amount": "0", "currency": "USD"}
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
        async with c.stream("GET", f"/api/v1/runs/{run['run_id']}/events") as r:
            events = [json.loads(line[5:]) async for line in r.aiter_lines() if line.startswith("data:")]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "started" and kinds[-1] == "completed"
    assert [e["node_completed"]["node_id"] for e in events if e["type"] == "node_completed"] == \
        ["sales", "analyse", "render", "write", "done"]
    assert "tool_call" in kinds and "tool_result" in kinds
    # Resume from a sequence number: only later events are replayed.
    async with httpx.AsyncClient(base_url=env.http, headers={**headers(env.keys.dev), "Last-Event-ID": "5"}) as c:
        async with c.stream("GET", f"/api/v1/runs/{run['run_id']}/events") as r:
            later = [json.loads(line[5:]) async for line in r.aiter_lines() if line.startswith("data:")]
    assert later[0]["seq"] == 6 and later[-1]["type"] == "completed"


async def test_inference_with_agent_shorthand(env):
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev), timeout=60) as c:
        r = await c.post("/api/v1/inference", json={"model": "local:default", "input": "Returns?",
                                                    "agent": "in_app_assistant"})
    assert r.status_code == 200, r.text
    assert r.json()["run_id"].startswith("run_") and r.json()["content"].startswith("echo:")


async def test_idempotent_run_start(env):
    body = {"agent_id": "in_app_assistant", "input": {"prompt": "idem"}, "idempotency_key": "run-key-1"}
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
        a, b = await asyncio.gather(c.post("/api/v1/agent/run", json=body), c.post("/api/v1/agent/run", json=body))
        assert a.json()["run_id"] == b.json()["run_id"]
        body["input"]["prompt"] = "different"
        r = await c.post("/api/v1/agent/run", json=body)
        assert r.status_code == 409


async def test_input_schema_enforced(env):
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
        r = await c.post("/api/v1/agent/run", json={"agent_id": "report_generator",
                                                    "input": {"report_name": "../../etc"}})
    assert r.status_code == 400


async def test_loop_condition_and_limits(env):
    loop_agent = agent("counter", [
        {"id": "init", "type": "transform", "set": {"n": 0}, "value": 0},
        {"id": "repeat", "type": "loop", "body": "inc", "max_iterations": 5, "while": "${ memory.n < 3 }"},
        {"id": "inc", "type": "transform", "set": {"n": "${ memory.n + 1 }"}, "value": "${ memory.n + 1 }"},
        {"id": "check", "type": "condition"},
        {"id": "big", "type": "return", "value": "${ {'n': memory.n, 'loop': nodes.repeat.output} }"},
        {"id": "small", "type": "return", "value": "small"},
    ], [{"from": "init", "to": "repeat"}, {"from": "inc", "to": "repeat"}, {"from": "repeat", "to": "check"},
        {"from": "check", "to": "big", "when": "${ memory.n >= 3 }"}, {"from": "check", "to": "small"}])
    assert (await register(env, loop_agent)).status_code == 200
    run = await run_to_end(env, "counter", {})
    assert run["status"] == "succeeded" and run["output"] == {"n": 3, "loop": {"iteration": 3, "exhausted": False}}

    runaway = copy.deepcopy(loop_agent)
    runaway["metadata"]["id"] = "runaway"
    runaway["spec"]["nodes"][1]["while"] = "${ true }"
    runaway["spec"]["nodes"][1]["max_iterations"] = 20
    runaway["spec"]["limits"] = {"max_steps": 12}
    assert (await register(env, runaway)).status_code == 200
    run = await run_to_end(env, "runaway", {})
    assert run["status"] == "failed" and run["error"]["code"] == "limit_exceeded"


async def test_model_output_schema_and_retry_failure(env):
    d = agent("strict_json", [
        {"id": "ask", "type": "llm", "model": "local:default", "prompt": "count",
         "output_schema": {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}}},
        {"id": "done", "type": "return", "value": "${ nodes.ask.output }"}], [{"from": "ask", "to": "done"}])
    assert (await register(env, d)).status_code == 200
    run = await run_to_end(env, "strict_json", {})
    assert run["status"] == "succeeded" and run["output"] == {"n": 1}


async def test_parallel_node_and_write_restriction(env):
    d = agent("fanout", [
        {"id": "both", "type": "parallel", "max_concurrency": 2, "branches": {
            "faq": {"type": "tool", "tool": "fs.read_text", "args": {"path": "docs/faq.md"}},
            "answer": {"type": "llm", "model": "local:default", "prompt": "hi"},
            "k": {"type": "transform", "value": 7}}},
        {"id": "done", "type": "return", "value": "${ {'k': nodes.both.output.k, 'faq': "
                                                   "nodes.both.output.faq.content.startsWith('# FAQ')} }"}],
        [{"from": "both", "to": "done"}], perms={"models": ["local:*"], "tools": ["fs.read_text"]})
    assert (await register(env, d)).status_code == 200
    run = await run_to_end(env, "fanout", {})
    assert run["status"] == "succeeded" and run["output"] == {"k": 7, "faq": True}, run

    w = agent("fanout_write", [
        {"id": "both", "type": "parallel", "branches": {
            "w": {"type": "tool", "tool": "fs.write_text", "args": {"path": "reports/p.md", "content": "x"}}}},
        {"id": "done", "type": "return", "value": 1}], [{"from": "both", "to": "done"}],
        perms={"models": [], "tools": ["fs.write_text"]})
    assert (await register(env, w)).status_code == 200
    run = await run_to_end(env, "fanout_write", {})
    assert run["status"] == "failed" and "parallel" in run["error"]["message"]
    assert not (env.ws / "reports" / "p.md").exists()


async def test_subagent_cannot_exceed_parent_permissions(env):
    child = agent("child_writer", [
        {"id": "w", "type": "tool", "tool": "fs.write_text",
         "args": {"path": "reports/${ input.name }.md", "content": "from child"}},
        {"id": "done", "type": "return", "value": "${ {'wrote': input.name} }"}], [{"from": "w", "to": "done"}],
        perms={"tools": ["fs.write_text"]})
    reader_parent = agent("parent_reader", [
        {"id": "call", "type": "agent", "agent": "child_writer", "input": {"name": "blocked"}},
        {"id": "done", "type": "return", "value": "${ nodes.call.output }"}], [{"from": "call", "to": "done"}],
        perms={"tools": ["fs.read_text"], "agents": ["child_writer"]})
    writer_parent = copy.deepcopy(reader_parent)
    writer_parent["metadata"]["id"] = "parent_writer"
    writer_parent["spec"]["nodes"][0]["input"] = {"name": "allowed"}
    writer_parent["spec"]["permissions"]["tools"] = ["fs.*"]
    for d in (child, reader_parent, writer_parent):
        assert (await register(env, d)).status_code == 200
    run = await run_to_end(env, "parent_reader", {})
    assert run["status"] == "failed" and "not permitted by a parent" in run["error"]["message"], run
    assert not (env.ws / "reports" / "blocked.md").exists()
    run = await run_to_end(env, "parent_writer", {})
    assert run["status"] == "succeeded" and run["output"] == {"wrote": "allowed"}
    assert (env.ws / "reports" / "allowed.md").exists()


async def test_cancellation_of_running_node(env):
    slow = agent("slow", [
        {"id": "think", "type": "llm", "model": "local:fake/echo", "prompt": "slow"},
        {"id": "done", "type": "return", "value": 1}], [{"from": "think", "to": "done"}])
    assert (await register(env, slow)).status_code == 200
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
        orig = env.svc.router.adapters["fake"].chat

        async def slow_chat(req):
            await asyncio.sleep(30)
            return await orig(req)
        env.svc.router.adapters["fake"].chat = slow_chat
        try:
            rid = (await c.post("/api/v1/agent/run", json={"agent_id": "slow", "input": {}})).json()["run_id"]
            for _ in range(50):
                if (await c.get(f"/api/v1/runs/{rid}")).json()["status"] == "running":
                    break
                await asyncio.sleep(0.1)
            await asyncio.sleep(0.3)
            t0 = asyncio.get_running_loop().time()
            r = await c.post(f"/api/v1/runs/{rid}/cancel", json={"reason": "test"})
            assert r.status_code == 200
            r2 = await c.post(f"/api/v1/runs/{rid}/cancel", json={})  # idempotent
            assert r2.status_code == 200
            for _ in range(50):
                run = (await c.get(f"/api/v1/runs/{rid}")).json()
                if run["status"] == "cancelled":
                    break
                await asyncio.sleep(0.1)
            assert run["status"] == "cancelled"
            assert asyncio.get_running_loop().time() - t0 < 5, "in-flight node was not interrupted"
        finally:
            env.svc.router.adapters["fake"].chat = orig


# ------------------------------------------------------------------ recovery (manual worker)

WRITER = agent("writer", [
    {"id": "prep", "type": "transform", "value": "${ 'content for ' + input.name }"},
    {"id": "write", "type": "tool", "tool": "fs.write_text",
     "args": {"path": "reports/${ input.name }.md", "content": "${ nodes.prep.output }"}},
    {"id": "done", "type": "return", "value": "${ {'path': nodes.write.output.path} }"}],
    [{"from": "prep", "to": "write"}, {"from": "write", "to": "done"}], perms={"tools": ["fs.write_text"]})


async def _start(m, name):
    p = dev(m)
    await m.svc.runs.register_agent(p, WRITER)
    row = await m.svc.runs.start_run(p, "writer", "", {"name": name})
    return p, row["run_id"]


async def _claim(m, run_id):
    row = await m.svc.engine.claim()
    assert row and row["run_id"] == run_id
    return row


async def _expire(m, run_id):
    await m.svc.store.execute("UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE run_id=%s", run_id)


async def test_resume_between_nodes_does_not_repeat(manual):
    m = manual
    p, rid = await _start(m, "between")
    m.svc.engine.crash_at = {"between_nodes"}
    row = await _claim(m, rid)
    with pytest.raises(LeaseLost):
        await m.svc.engine.execute(row, row["lease_version"])
    m.svc.engine.crash_at = set()
    await _expire(m, rid)
    row = await _claim(m, rid)
    assert row["checkpoint"]["next"] == "write" and row["attempts"] == 2
    result = await m.svc.engine.execute(row, row["lease_version"])
    assert result["status"] == "succeeded"
    events = await m.svc.runs.events(p, rid)
    done = [e["body"]["node_completed"]["node_id"] for e in events if "node_completed" in e["body"]]
    assert done == ["prep", "write", "done"], "no node may run twice"


async def test_crash_after_write_reuses_recorded_outcome(manual):
    m = manual
    p, rid = await _start(m, "after_tool")
    m.svc.engine.crash_at = {"after_tool"}
    row = await _claim(m, rid)
    with pytest.raises(LeaseLost):
        await m.svc.engine.execute(row, row["lease_version"])
    m.svc.engine.crash_at = set()
    f = m.ws / "reports" / "after_tool.md"
    assert f.read_text(encoding="utf-8") == "content for after_tool"
    await _expire(m, rid)
    row = await _claim(m, rid)
    result = await m.svc.engine.execute(row, row["lease_version"])
    assert result["status"] == "succeeded", result
    intents = await m.svc.store.fetchall("SELECT status FROM tool_intents WHERE run_id=%s", rid)
    assert [i["status"] for i in intents] == ["completed"], "the write must not be repeated"


async def test_ambiguous_write_needs_attention_then_resolve(manual):
    m = manual
    p, rid = await _start(m, "ambiguous")
    m.svc.engine.crash_at = {"after_intent"}
    row = await _claim(m, rid)
    with pytest.raises(LeaseLost):
        await m.svc.engine.execute(row, row["lease_version"])
    m.svc.engine.crash_at = set()
    await _expire(m, rid)
    row = await _claim(m, rid)
    result = await m.svc.engine.execute(row, row["lease_version"])
    assert result["status"] == "needs_attention"
    run = await m.svc.runs.get_run(p, rid)
    assert run["error"]["code"] == "failed_precondition" and "intent_id" in run["error"]["details"]
    assert not (m.ws / "reports" / "ambiguous.md").exists()
    with pytest.raises(UARError) as ei:
        await m.svc.runs.resolve(p, rid, "retry_node")  # developers lack runs:resolve
    assert ei.value.code == "permission_denied"
    await m.svc.runs.resolve(ops(m), rid, "retry_node", "verified nothing was written")
    row = await _claim(m, rid)
    result = await m.svc.engine.execute(row, row["lease_version"])
    assert result["status"] == "succeeded"
    assert (m.ws / "reports" / "ambiguous.md").exists()
    statuses = sorted(i["status"] for i in await m.svc.store.fetchall(
        "SELECT status FROM tool_intents WHERE run_id=%s", rid))
    assert statuses == ["completed", "resolved"]


async def test_stale_worker_cannot_commit(manual):
    m = manual
    p, rid = await _start(m, "fenced")
    row = await _claim(m, rid)
    stale_fence = row["lease_version"]
    await _expire(m, rid)
    fresh = await _claim(m, rid)
    assert fresh["lease_version"] == stale_fence + 1
    with pytest.raises(LeaseLost):
        await m.svc.engine.execute(row, stale_fence)
    # Each fenced write path rejects the stale token on its own.
    state = {"next": "write", "steps": 1, "tokens": {"input": 0, "output": 0}, "cost": "0"}
    with pytest.raises(LeaseLost):
        await m.svc.engine._commit(rid, stale_fence, state, [])
    with pytest.raises(LeaseLost):
        await m.svc.engine._event(rid, stale_fence, {"node_started": {"node_id": "x"}})
    result = await m.svc.engine.execute(fresh, fresh["lease_version"])
    assert result["status"] == "succeeded"


async def test_revoked_credential_stops_run(manual):
    m = manual
    p, rid = await _start(m, "revoked")
    row = await _claim(m, rid)
    kid = p.key_id
    saved = m.svc.auth.keys.pop(kid)
    try:
        result = await m.svc.engine.execute(row, row["lease_version"])
    finally:
        m.svc.auth.keys[kid] = saved
    assert result["status"] == "failed"
    assert not (m.ws / "reports" / "revoked.md").exists()
