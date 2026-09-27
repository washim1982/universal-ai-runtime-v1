"""M7 gates: plugin runtime (register -> validate -> activate -> pin -> rollback) with real plugin processes."""
from __future__ import annotations

import asyncio
import os
import copy
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml

from conftest import ROOT, headers
from uar_runtime.config import ToolPolicyCfg
from uar_runtime.errors import UARError


def echo_manifest(pid: str, version: str, *, kind: str = "model", extra_args=(), tag=None, provider="echoprov",
                  timeout_s=10, **spec_extra) -> dict:
    spec = {"type": kind, "runtime": {"api": "uar.plugin.v1", "mode": "process", "command": "python",
                                      "args": ["plugins/testing/echo_plugin.py", "--id", pid, "--version", version,
                                               "--kind", kind, *extra_args]},
            "capabilities": {"network": [], "secrets": []}, "limits": {"timeout_s": timeout_s, "concurrency": 4},
            "config": {"tag": tag or version}}
    if kind == "model":
        spec["provider"] = {"id": provider, "model_class": "enterprise", "capabilities": ["chat", "stream"]}
    if kind == "tool":
        spec["tool_namespace"] = "shouty"
        spec["tools"] = {"shout": {"side_effect": "read"}}
    spec.update(spec_extra)
    return {"apiVersion": "uar/v1", "kind": "Plugin", "metadata": {"id": pid, "version": version}, "spec": spec}


async def admin_post(env, path, body):
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.admin), timeout=60) as c:
        return await c.post(path, json=body)


async def register_and_activate(env, manifest):
    r = await admin_post(env, "/api/v1/plugins", {"manifest": manifest})
    assert r.status_code == 200, r.text
    m = manifest["metadata"]
    return await admin_post(env, f"/api/v1/plugins/{m['id']}/activate", {"version": m["version"]})


async def test_only_admins_manage_plugins(env):
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev)) as c:
        r = await c.post("/api/v1/plugins", json={"manifest": echo_manifest("acme.nope", "1.0.0")})
        assert r.status_code == 403
        assert (await c.get("/api/v1/plugins")).status_code == 403
    r = await admin_post(env, "/api/v1/plugins", {"manifest": {"apiVersion": "uar/v1", "kind": "Plugin"}})
    assert r.status_code == 400


async def test_model_plugin_serves_new_provider_without_gateway_changes(env):
    r = await register_and_activate(env, echo_manifest("acme.echo", "1.0.0", tag="one"))
    assert r.status_code == 200, r.text
    assert r.json()["active"] is True and r.json()["status"] == "active"
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev), timeout=60) as c:
        r = await c.post("/api/v1/inference", json={"model": "enterprise:echoprov/echo", "input": "hello plugin"})
        assert r.status_code == 200, r.text
        assert r.json()["content"] == "one: hello plugin" and r.json()["provider"] == "echoprov"
        async with c.stream("POST", "/api/v1/inference", json={"model": "enterprise:echoprov/echo",
                                                               "input": "a b c", "stream": True}) as s:
            text = "".join(__import__("json").loads(l[5:])["token"]["text"] for l in [x async for x in s.aiter_lines()]
                           if l.startswith("data:") and '"token"' in l)
        assert text == "one: a b c"
        models = {m["name"] for m in (await c.get("/api/v1/models")).json()["models"]}
        assert "enterprise:echoprov/echo" in models
    listed = (await httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.admin)).get("/api/v1/plugins")).json()
    assert any(p["plugin_id"] == "acme.echo" and p["active"] for p in listed["plugins"])


async def test_versions_are_immutable(env):
    m = echo_manifest("acme.immut", "1.0.0")
    assert (await admin_post(env, "/api/v1/plugins", {"manifest": m})).status_code == 200
    assert (await admin_post(env, "/api/v1/plugins", {"manifest": m})).status_code == 200
    m2 = copy.deepcopy(m)
    m2["spec"]["config"]["tag"] = "changed"
    assert (await admin_post(env, "/api/v1/plugins", {"manifest": m2})).status_code == 409


async def test_undeclared_capability_refused(env):
    m = echo_manifest("acme.greedy", "1.0.0", extra_args=["--capability", "network:evil.example"])
    r = await register_and_activate(env, m)
    assert r.status_code == 409 and "undeclared capabilities" in r.json()["error"]["message"]
    rows = (await httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.admin)).get("/api/v1/plugins")).json()
    assert next(p for p in rows["plugins"] if p["plugin_id"] == "acme.greedy")["status"] == "failed"


async def test_artifact_integrity_checked(env, tmp_path):
    art = "plugins/testing/echo_plugin.py"
    good = "sha256:" + hashlib.sha256((ROOT / art).read_bytes()).hexdigest()
    m = echo_manifest("acme.signed", "1.0.0", provider="signedprov")
    m["spec"]["runtime"].update(artifact=art, digest="sha256:" + "0" * 64)
    r = await register_and_activate(env, m)
    assert r.status_code == 409 and "digest mismatch" in r.json()["error"]["message"]
    m["metadata"]["version"] = "1.0.1"
    m["spec"]["runtime"]["args"][4] = "1.0.1"
    m["spec"]["runtime"]["digest"] = good
    r = await register_and_activate(env, m)
    assert r.status_code == 200, r.text


async def test_tool_plugin_is_governed_like_mcp_tools(env):
    env.svc.s.tool_policies.append(ToolPolicyCfg(tool="shouty.*", roles=["developer", "admin"]))
    r = await register_and_activate(env, echo_manifest("acme.shout", "1.0.0", kind="tool"))
    assert r.status_code == 200, r.text
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev), timeout=60) as c:
        tools = {t["name"]: t for t in (await c.get("/api/v1/tools")).json()["tools"]}
        assert tools["shouty.shout"]["side_effect"] == "read" and tools["shouty.shout"]["server"] == "plugin:acme.shout"
        r = await c.post("/api/v1/tool/execute", json={"tool": "shouty.shout", "args": {"text": "hi"}})
        assert r.status_code == 200 and r.json()["structured"]["text"] == "HI" and r.json()["untrusted"]
        r = await c.post("/api/v1/tool/execute", json={"tool": "shouty.shout", "args": {"text": 5}})
        assert r.status_code == 400  # schema validation from the plugin's descriptor
        r = await c.post("/api/v1/tool/execute", json={"tool": "shouty.shout", "args": {"text": ""}})
        assert r.json()["is_error"] is True
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.ops)) as c:
        r = await c.post("/api/v1/tool/execute", json={"tool": "shouty.shout", "args": {"text": "x"}})
        assert r.status_code == 403  # tool policy still applies to plugin tools


async def test_agent_plugin_called_from_a_graph(env):
    manifest = yaml.safe_load((ROOT / "plugins/reference/py-agent/plugin.yaml").read_text(encoding="utf-8"))
    r = await register_and_activate(env, manifest)
    assert r.status_code == 200, r.text
    graph = {"apiVersion": "uar/v1", "kind": "Agent", "metadata": {"id": "uses_plugin_agent", "version": "1.0.0"},
             "spec": {"start": "sum", "permissions": {"agents": ["summarize_text"]},
                      "nodes": [{"id": "sum", "type": "agent", "agent": "summarize_text",
                                 "input": {"text": "${ input.text }"}},
                                {"id": "done", "type": "return", "value": "${ nodes.sum.output }"}],
                      "edges": [{"from": "sum", "to": "done"}]}}
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev), timeout=60) as c:
        assert (await c.post("/api/v1/agents", json={"definition": graph})).status_code == 200
        run = (await c.post("/api/v1/agent/run", json={"agent_id": "uses_plugin_agent", "input": {
            "text": "UAR runs agents. Plugins extend it. This sentence is dropped."}})).json()
        for _ in range(100):
            run = (await c.get(f"/api/v1/runs/{run['run_id']}")).json()
            if run["status"] not in ("queued", "running"):
                break
            await asyncio.sleep(0.1)
    assert run["status"] == "succeeded", run
    assert run["output"] == {"summary": "UAR runs agents. Plugins extend it.", "sentences": 3, "words": 10}


async def test_declarative_agent_plugin_registers_graph_for_every_tenant(env):
    graph = {"apiVersion": "uar/v1", "kind": "Agent", "metadata": {"id": "packaged_greeter", "version": "1.0.0"},
             "spec": {"start": "done", "nodes": [{"id": "done", "type": "return",
                                                   "value": "${ {'greeting': 'hello ' + input.name} }"}], "edges": []}}
    m = {"apiVersion": "uar/v1", "kind": "Plugin", "metadata": {"id": "acme.greeter", "version": "1.0.0"},
         "spec": {"type": "agent", "graph": graph, "runtime": {"mode": "none"}}}
    r = await register_and_activate(env, m)
    assert r.status_code == 200, r.text
    for key in (env.keys.dev, env.keys.other_tenant):
        async with httpx.AsyncClient(base_url=env.http, headers=headers(key)) as c:
            run = (await c.post("/api/v1/agent/run", json={"agent_id": "packaged_greeter", "input": {"name": "Ada"}})).json()
            for _ in range(50):
                run = (await c.get(f"/api/v1/runs/{run['run_id']}")).json()
                if run["status"] == "succeeded":
                    break
                await asyncio.sleep(0.1)
            assert run["output"] == {"greeting": "hello Ada"}
    bad = copy.deepcopy(m)
    bad["metadata"]["id"] = "acme.bad"
    bad["spec"]["type"] = "tool"
    bad["spec"]["tool_namespace"] = "x"
    bad["spec"].pop("graph")
    assert (await admin_post(env, "/api/v1/plugins", {"manifest": bad})).status_code == 400


async def test_upgrade_pins_running_runs_and_rollback(manual):
    m = manual
    admin = m.svc.auth.authenticate(m.keys.admin, None)
    dev = m.svc.auth.authenticate(m.keys.dev, None)
    await m.svc.plugins.register(admin, echo_manifest("acme.pin", "1.0.0", tag="one", provider="pinprov"))
    await m.svc.plugins.register(admin, echo_manifest("acme.pin", "2.0.0", tag="two", provider="pinprov"))
    await m.svc.plugins.activate(admin, "acme.pin", "1.0.0")
    graph = {"apiVersion": "uar/v1", "kind": "Agent", "metadata": {"id": "pinned", "version": "1.0.0"},
             "spec": {"start": "ask", "permissions": {"models": ["enterprise:*"]},
                      "nodes": [{"id": "ask", "type": "llm", "model": "enterprise:pinprov/echo", "prompt": "hi"},
                                {"id": "done", "type": "return", "value": "${ nodes.ask.output }"}],
                      "edges": [{"from": "ask", "to": "done"}]}}
    await m.svc.runs.register_agent(dev, graph)
    old = await m.svc.runs.start_run(dev, "pinned", "", {})
    assert old["plugins"]["acme.pin"] == "1.0.0"
    upgraded = await m.svc.plugins.activate(admin, "acme.pin", "2.0.0")     # upgrade while `old` is queued
    assert upgraded["previous_version"] == "1.0.0"
    new = await m.svc.runs.start_run(dev, "pinned", "", {})

    async def run_to_end(run_id):
        row = await m.svc.engine.claim()
        assert row["run_id"] == run_id
        await m.svc.engine.execute(row, row["lease_version"])
        return await m.svc.runs.get_run(dev, run_id)
    assert (await run_to_end(old["run_id"]))["output"]["text"] == "one: hi"   # pinned to 1.0.0
    assert (await run_to_end(new["run_id"]))["output"]["text"] == "two: hi"   # started after the upgrade
    rolled = await m.svc.plugins.rollback(admin, "acme.pin")
    assert rolled["version"] == "1.0.0" and rolled["active"]
    after = await m.svc.runs.start_run(dev, "pinned", "", {})
    assert (await run_to_end(after["run_id"]))["output"]["text"] == "one: hi"
    with pytest.raises(UARError):
        await m.svc.plugins.rollback(dev, "acme.pin")     # developers cannot manage plugins


async def test_hanging_plugin_times_out_then_circuit_opens(env):
    r = await register_and_activate(env, echo_manifest("acme.hang", "1.0.0", extra_args=["--hang"],
                                                       provider="hangprov", timeout_s=1))
    assert r.status_code == 200, r.text
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev), timeout=60) as c:
        codes = [(await c.post("/api/v1/inference", json={"model": "enterprise:hangprov/echo", "input": "x"})).json()
                 ["error"]["code"] for _ in range(4)]
    assert codes[:3] == ["deadline_exceeded"] * 3 and codes[3] == "unavailable", codes


GO_PLUGIN = ROOT / "plugins/reference/go-model"
TS_PLUGIN = ROOT / "plugins/reference/ts-tool"


@pytest.mark.skipif(not shutil.which("go"), reason="Go toolchain not installed")
async def test_go_model_plugin(env):
    exe = GO_PLUGIN / ("sentiment.exe" if sys.platform == "win32" else "sentiment")
    if not exe.exists():
        r = await asyncio.to_thread(subprocess.run, ["go", "build", "-o", exe.name, "."], cwd=GO_PLUGIN,
                                    capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    manifest = yaml.safe_load((GO_PLUGIN / "plugin.yaml").read_text(encoding="utf-8"))
    manifest["spec"]["runtime"]["command"] = str(exe.relative_to(ROOT)).replace("\\", "/")
    manifest["spec"]["runtime"].pop("artifact", None)
    manifest["spec"]["runtime"].pop("digest", None)
    r = await register_and_activate(env, manifest)
    assert r.status_code == 200, r.text
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev), timeout=60) as c:
        r = await c.post("/api/v1/inference", json={"model": "enterprise:acme/sentiment",
                                                    "input": "I love this great product"})
        assert r.status_code == 200, r.text
        assert r.json()["content"] == "positive" and r.json()["provider"] == "acme"


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js not installed")
async def test_typescript_tool_plugin(env):
    if not (TS_PLUGIN / "node_modules").exists():
        pytest.skip("run `npm install` in plugins/reference/ts-tool first")
    env.svc.s.tool_policies.append(ToolPolicyCfg(tool="text.*", roles=["developer", "admin"]))
    manifest = yaml.safe_load((TS_PLUGIN / "plugin.yaml").read_text(encoding="utf-8"))
    r = await register_and_activate(env, manifest)
    assert r.status_code == 200, r.text
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev), timeout=60) as c:
        r = await c.post("/api/v1/tool/execute", json={"tool": "text.word_count", "args": {"text": "one two three"}})
        assert r.status_code == 200 and r.json()["structured"] == {"words": 3, "characters": 13}, r.text
        r = await c.post("/api/v1/tool/execute", json={"tool": "text.slugify", "args": {"text": "Hello, World!"}})
        assert r.json()["structured"] == {"slug": "hello-world"}


DOTNET_PLUGIN = ROOT / "plugins/reference/dotnet-tool"
JAVA_PLUGIN = ROOT / "plugins/reference/java-agent"


def _java() -> str | None:
    found = shutil.which("java")
    if found:
        return found
    for base in (Path(os.environ.get("JAVA_HOME", "")), *sorted(Path(r"C:\Program Files\Microsoft").glob("jdk-*"))):
        exe = base / "bin" / ("java.exe" if sys.platform == "win32" else "java")
        if str(base) and exe.exists():
            return str(exe)
    return None


@pytest.mark.skipif(not shutil.which("dotnet"), reason=".NET SDK not installed")
async def test_dotnet_tool_plugin(env):
    dll = DOTNET_PLUGIN / "bin/Release/net8.0/MathUtil.dll"
    if not dll.exists():
        r = await asyncio.to_thread(subprocess.run, ["dotnet", "build", "-c", "Release", str(DOTNET_PLUGIN)],
                                    capture_output=True, text=True)
        assert r.returncode == 0, r.stdout[-2000:]
    env.svc.s.tool_policies.append(ToolPolicyCfg(tool="math.*", roles=["developer", "admin"]))
    manifest = yaml.safe_load((DOTNET_PLUGIN / "plugin.yaml").read_text(encoding="utf-8"))
    r = await register_and_activate(env, manifest)
    assert r.status_code == 200, r.text
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev), timeout=60) as c:
        r = await c.post("/api/v1/tool/execute", json={"tool": "math.stats", "args": {"numbers": [3, 1, 2, 10]}})
        assert r.status_code == 200, r.text
        assert r.json()["structured"] == {"count": 4, "sum": 16, "mean": 4, "median": 2.5, "min": 1, "max": 10}


@pytest.mark.skipif(not _java(), reason="Java not installed")
async def test_java_agent_plugin(env):
    jar = JAVA_PLUGIN / "target/java-agent.jar"
    if not jar.exists():
        pytest.skip("build it: mvn -f plugin-sdks/java install && mvn -f plugins/reference/java-agent package")
    manifest = yaml.safe_load((JAVA_PLUGIN / "plugin.yaml").read_text(encoding="utf-8"))
    manifest["spec"]["runtime"]["command"] = _java()
    r = await register_and_activate(env, manifest)
    assert r.status_code == 200, r.text
    graph = {"apiVersion": "uar/v1", "kind": "Agent", "metadata": {"id": "uses_java_agent", "version": "1.0.0"},
             "spec": {"start": "wf", "permissions": {"agents": ["word_frequency"]},
                      "nodes": [{"id": "wf", "type": "agent", "agent": "word_frequency", "input": {"text": "${ input.text }"}},
                                {"id": "done", "type": "return", "value": "${ nodes.wf.output }"}],
                      "edges": [{"from": "wf", "to": "done"}]}}
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev), timeout=60) as c:
        assert (await c.post("/api/v1/agents", json={"definition": graph})).status_code == 200
        run = (await c.post("/api/v1/agent/run", json={"agent_id": "uses_java_agent",
                                                       "input": {"text": "UAR runs agents and UAR runs tools; agents plan"}})).json()
        for _ in range(100):
            run = (await c.get(f"/api/v1/runs/{run['run_id']}")).json()
            if run["status"] not in ("queued", "running"):
                break
            await asyncio.sleep(0.1)
    assert run["status"] == "succeeded", run
    assert run["output"]["top"] == [{"word": "agents", "count": 2}, {"word": "runs", "count": 2},
                                    {"word": "uar", "count": 2}]
