"""M5 gate: one run = one connected trace; metrics agree with the usage ledger."""
from __future__ import annotations

import asyncio

import httpx
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client.parser import text_string_to_metric_families

from conftest import headers
from uar_runtime.observability import setup_tracing


def metric_value(text: str, name: str, labels: dict) -> float:
    for fam in text_string_to_metric_families(text):
        for s in fam.samples:
            if s.name == name and all(s.labels.get(k) == v for k, v in labels.items()):
                return s.value
    return 0.0


async def test_agent_run_is_one_connected_trace(env):
    exporter = InMemorySpanExporter()
    setup_tracing("uar-test", None, exporter=exporter)
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
        r = await c.post("/api/v1/agent/run", json={"agent_id": "report_generator",
                                                    "input": {"report_name": "traced"}})
        rid = r.json()["run_id"]
        for _ in range(100):
            if (await c.get(f"/api/v1/runs/{rid}")).json()["status"] == "succeeded":
                break
            await asyncio.sleep(0.1)
    await asyncio.sleep(0.2)
    spans = exporter.get_finished_spans()
    run_span = next(s for s in spans if s.name == "agent.run" and s.attributes.get("uar.run_id") == rid)
    trace_id = run_span.context.trace_id
    in_trace = {s.name for s in spans if s.context.trace_id == trace_id}
    assert "POST /api/v1/agent/run" in in_trace, "worker spans must join the request's trace"
    assert {"node sales", "node analyse", "node write", "model.chat", "tool.execute"} <= in_trace, in_trace
    chat = next(s for s in spans if s.name == "model.chat" and s.context.trace_id == trace_id)
    assert chat.attributes["gen_ai.system"] == "fake" and chat.attributes["gen_ai.usage.output_tokens"] > 0


async def test_metrics_match_ledger(env):
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
        before = (await c.get("/metrics")).text
        r = await c.post("/api/v1/inference", json={"model": "local:fake/echo-large", "input": "count these tokens"})
        after = (await c.get("/metrics")).text
    row = await env.svc.store.fetchone("SELECT input_tokens, output_tokens FROM usage_ledger WHERE request_id=%s",
                                       r.headers["x-request-id"])
    labels = {"provider": "fake", "model": "echo-large"}
    d_in = metric_value(after, "uar_model_tokens_total", {**labels, "direction": "input"}) - \
        metric_value(before, "uar_model_tokens_total", {**labels, "direction": "input"})
    d_out = metric_value(after, "uar_model_tokens_total", {**labels, "direction": "output"}) - \
        metric_value(before, "uar_model_tokens_total", {**labels, "direction": "output"})
    assert (d_in, d_out) == (row["input_tokens"], row["output_tokens"])
    assert metric_value(after, "uar_requests_total", {"transport": "http", "operation": "/api/v1/inference",
                                                      "code": "200"}) >= 1


async def test_simulation_decides_argument_policies(env):
    p = env.svc.auth.authenticate(env.keys.dev, None)
    d = {"apiVersion": "uar/v1", "kind": "Agent", "metadata": {"id": "sim_policy", "version": "1.0.0"},
         "spec": {"start": "w", "permissions": {"tools": ["fs.write_text"]},
                  "nodes": [{"id": "w", "type": "tool", "tool": "fs.write_text",
                             "args": {"path": "${ input.dir + '/x.md' }", "content": "x"}},
                            {"id": "end", "type": "return", "value": 1}],
                  "edges": [{"from": "w", "to": "end"}]}}
    static = await env.svc.dry_run(p, {"definition": d}, "r1")
    assert any("depend on runtime values" in u for u in static["unresolved"])
    denied = await env.svc.dry_run(p, {"definition": d, "mode": "simulate", "input": {"dir": "docs"},
                                       "fixtures": {"w": {"ok": True}}}, "r2")
    assert not denied["valid"] and "outside allowed prefixes" in denied["errors"][0]
    allowed = await env.svc.dry_run(p, {"definition": d, "mode": "simulate", "input": {"dir": "reports"},
                                        "fixtures": {"w": {"ok": True}}}, "r3")
    assert allowed["valid"] and not allowed["unresolved"], allowed
