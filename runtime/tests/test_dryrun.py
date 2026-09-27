"""M5 gate: dry-run never executes anything and never invents results."""
from __future__ import annotations

import ast
import asyncio
import subprocess
from pathlib import Path

import httpx
import pytest

from conftest import headers
from uar_runtime.mcp import orchestrator as orch_mod
from uar_runtime.router.adapters.fake import FakeAdapter

PKG = Path(__file__).resolve().parents[1] / "uar_runtime"
FORBIDDEN_INTERNAL = ("uar_runtime.router.adapters", "uar_runtime.router.service", "uar_runtime.mcp",
                      "uar_runtime.engine.executor", "uar_runtime.service", "uar_runtime.gateway")
FORBIDDEN_EXTERNAL = ("httpx", "mcp", "anthropic", "subprocess", "socket", "grpc", "urllib")


def _imports(module: str) -> set[str]:
    path = PKG.parent / (module.replace(".", "/") + ".py")
    if not path.exists():
        path = PKG.parent / module.replace(".", "/") / "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    pkg_parts = module.split(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg_parts[: len(pkg_parts) - node.level + (1 if path.name == "__init__.py" else 0)]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            out.add(mod)
            out |= {f"{mod}.{a.name}" for a in node.names}
    return out


def test_simulation_import_graph_is_execution_free():
    seen, stack, external = set(), ["uar_runtime.simulation.planner"], set()
    while stack:
        m = stack.pop()
        if m in seen:
            continue
        seen.add(m)
        for imp in _imports(m):
            if imp.startswith("uar_runtime"):
                assert not imp.startswith(FORBIDDEN_INTERNAL), f"{m} imports {imp}"
                candidate = imp
                while candidate and not ((PKG.parent / (candidate.replace(".", "/") + ".py")).exists()
                                         or (PKG.parent / candidate.replace(".", "/") / "__init__.py").exists()):
                    candidate = candidate.rsplit(".", 1)[0] if "." in candidate else ""
                if candidate:
                    stack.append(candidate)
            elif m.startswith("uar_runtime.simulation"):
                external.add(imp.split(".")[0])
    assert not external & set(FORBIDDEN_EXTERNAL), external


@pytest.fixture
def tripwires(monkeypatch):
    """Make every execution entry point fail loudly if reached."""
    calls = []

    def trip(name):
        def f(*a, **k):
            calls.append(name)
            raise AssertionError(f"dry-run reached {name}")
        return f

    async def atrip(*a, **k):
        calls.append("async")
        raise AssertionError("dry-run reached an async execution entry point")

    monkeypatch.setattr(FakeAdapter, "chat", atrip)
    monkeypatch.setattr(FakeAdapter, "stream", trip("FakeAdapter.stream"))
    monkeypatch.setattr(orch_mod.Orchestrator, "execute", atrip)
    monkeypatch.setattr(orch_mod.ServerSession, "call", atrip)
    monkeypatch.setattr(orch_mod.ServerSession, "ensure", atrip)
    monkeypatch.setattr(subprocess, "Popen", trip("subprocess.Popen"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", atrip)
    monkeypatch.setattr(httpx.AsyncClient, "send", atrip)
    return calls


async def dry(env, body, key=None):
    async with httpx.AsyncClient(base_url=env.http, headers=headers(key or env.keys.dev)) as c:
        return await c.post("/api/v1/dry-run", json=body)


async def test_static_plan_executes_nothing(manual, tripwires):
    env = manual
    # Called through the service: the HTTP test client itself would trip the httpx tripwire.
    reports_before = set((env.ws / "reports").iterdir())
    p = env.svc.auth.authenticate(env.keys.dev, None)
    rep = await env.svc.dry_run(p, {"agent_id": "report_generator", "mode": "static"}, "req_dry_static")
    assert rep["valid"] and rep["executed_nothing"] and not tripwires
    assert [s["node_id"] for s in rep["steps"]] == ["sales", "analyse", "render", "write", "done"]
    assert rep["routes"][0]["provider"] == "fake"
    assert rep["branches"] == ["sales -> analyse -> render -> write -> done"]
    assert "tools:execute" in rep["permissions_required"] and rep["permissions_missing"] == []
    assert rep["estimated_output_tokens_max"] == 900 and rep["estimated_cost_max"]["amount"] == "0"
    assert any("durable intent" in w for w in rep["warnings"])
    assert set((env.ws / "reports").iterdir()) == reports_before


async def test_simulation_with_fixtures(manual, tripwires):
    env = manual
    p = env.svc.auth.authenticate(env.keys.dev, None)
    fixtures = {"sales": {"columns": ["region"], "rows": [["North"]]},
                "analyse": {"title": "Simulated", "body": "- ok"},
                "write": {"path": "reports/sim.md", "bytes": 10, "created": True}}
    rep = await env.svc.dry_run(p, {"agent_id": "report_generator", "mode": "simulate",
                                    "input": {"report_name": "sim"}, "fixtures": fixtures, "seed": 7}, "req_sim")
    assert rep["valid"] and not rep["unresolved"] and not tripwires, rep
    done = rep["steps"][-1]
    assert done["status"] == "simulated" and done["output"] == {"path": "reports/sim.md", "title": "Simulated"}
    render = next(s for s in rep["steps"] if s["node_id"] == "render")
    assert render["output"] == {"value": "# Simulated\n\n- ok\n"}
    assert not (env.ws / "reports" / "sim.md").exists()


async def test_missing_fixture_is_unresolved_not_invented(manual, tripwires):
    env = manual
    p = env.svc.auth.authenticate(env.keys.dev, None)
    rep = await env.svc.dry_run(p, {"agent_id": "report_generator", "mode": "simulate",
                                    "input": {"report_name": "x"},
                                    "fixtures": {"sales": {"columns": [], "rows": []}}}, "req_sim2")
    statuses = {s["node_id"]: s["status"] for s in rep["steps"]}
    assert statuses == {"sales": "simulated", "analyse": "unresolved"}
    assert any("no fixture" in u for u in rep["unresolved"])
    assert "output" not in rep["steps"][-1]
    rep = await env.svc.dry_run(p, {"agent_id": "report_generator", "mode": "simulate", "input": {"report_name": "x"},
                                    "fixtures": {"sales": {"columns": [], "rows": []},
                                                 "analyse": {"title": 5}}}, "req_sim3")
    assert not rep["valid"] and "does not match output_schema" in rep["errors"][0]


async def test_denials_and_unknowns_are_reported(manual, tripwires):
    env = manual
    ops = env.svc.auth.authenticate(env.keys.ops, None)
    rep = await env.svc.dry_run(ops, {"agent_id": "report_generator"}, "req_d1")
    assert set(rep["permissions_missing"]) >= {"runs:start", "tools:execute", "inference:local"}
    dev = env.svc.auth.authenticate(env.keys.dev, None)
    rep = await env.svc.dry_run(dev, {"inference": {"model": "cloud:default"}}, "req_d2")
    assert not rep["valid"] and "inference:cloud" in rep["errors"][0]
    rep = await env.svc.dry_run(dev, {"inference": {"model": "local:fake_b/other"}}, "req_d3")
    assert rep["valid"] and "estimated_cost_max" not in rep
    assert any("no price" in u for u in rep["unresolved"])
    rep = await env.svc.dry_run(dev, {"definition": {"apiVersion": "uar/v1"}}, "req_d4")
    assert rep["valid"] is False and rep["errors"]
    assert not tripwires


async def test_dry_run_over_http_and_audit(env):
    r = await dry(env, {"agent_id": "in_app_assistant", "mode": "static"})
    assert r.status_code == 200 and r.json()["executed_nothing"] is True
    rows = await env.svc.store.fetchall("SELECT action FROM audit WHERE request_id=%s", r.headers["x-request-id"])
    assert [x["action"] for x in rows] == ["dryrun"]
    r = await dry(env, {"agent_id": "in_app_assistant"}, key=env.keys.viewer)
    assert r.status_code == 403


async def test_loop_branches_and_visit_multipliers(env):
    p = env.svc.auth.authenticate(env.keys.dev, None)
    d = {"apiVersion": "uar/v1", "kind": "Agent", "metadata": {"id": "plan_loop", "version": "1.0.0"},
         "spec": {"start": "l", "permissions": {"models": ["local:*"]},
                  "nodes": [{"id": "l", "type": "loop", "body": "ask", "max_iterations": 4, "while": "${ true }"},
                            {"id": "ask", "type": "llm", "model": "local:default", "prompt": "x",
                             "params": {"max_tokens": 10}},
                            {"id": "end", "type": "return", "value": 1}],
                  "edges": [{"from": "ask", "to": "l"}, {"from": "l", "to": "end"}]}}
    rep = await env.svc.dry_run(p, {"definition": d}, "req_loop")
    assert rep["estimated_output_tokens_max"] == 40
    assert sorted(rep["branches"]) == ["l -> ask -> l(exit) -> end", "l -> l(exit) -> end"]
