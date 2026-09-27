"""Live gates against real local model servers (run with: pytest -m live).

Requires Ollama (granite4:latest), llama.cpp llama-server and LM Studio on their default ports.
Each test skips if its server is unreachable; results are recorded in docs/gates/.
"""
from __future__ import annotations

import json
import os

import httpx
import pytest

from conftest import headers, start_env, stop_env

pytestmark = pytest.mark.live

OLLAMA = os.environ.get("UAR_OLLAMA_URL", "http://127.0.0.1:11434")
LMSTUDIO = os.environ.get("UAR_LMSTUDIO_URL", "http://127.0.0.1:1234/v1")
LLAMACPP = os.environ.get("UAR_LLAMACPP_URL", "http://127.0.0.1:8080/v1")
LMSTUDIO_MODEL = os.environ.get("UAR_LIVE_LMSTUDIO_MODEL", "qwen/qwen3.8-27b")
OLLAMA_MODEL = os.environ.get("UAR_LIVE_OLLAMA_MODEL", "granite4:latest")


@pytest.fixture(scope="module")
async def live(tmp_path_factory):
    name = f"uar_test_live_{os.getpid()}"
    over = dict(
        providers=[
            {"id": "ollama", "type": "ollama", "model_class": "local", "base_url": OLLAMA, "max_concurrency": 2},
            {"id": "lmstudio", "type": "openai_compat", "model_class": "local", "base_url": LMSTUDIO,
             "timeout_s": 600, "max_concurrency": 1},
            {"id": "llamacpp", "type": "openai_compat", "model_class": "local", "base_url": LLAMACPP,
             "timeout_s": 600, "max_concurrency": 1, "api_key_env": "UAR_LLAMACPP_KEY"},
        ],
        router={"default_class": "local", "classes": {"local": ["ollama", "lmstudio", "llamacpp"]},
                "aliases": {"local:default": f"ollama/{OLLAMA_MODEL}"}},
        pricing={"version": "live", "currency": "USD",
                 "models": {"ollama/*": {"input_per_mtok": "0", "output_per_mtok": "0"},
                            "lmstudio/*": {"input_per_mtok": "0", "output_per_mtok": "0"},
                            "llamacpp/*": {"input_per_mtok": "0", "output_per_mtok": "0"}}},
    )
    e, cleanup = await start_env(tmp_path_factory.mktemp("live"), name, **over)
    yield e
    await stop_env(e, cleanup, name)


def catalog(env, pid):
    models = env.svc.router.catalog.get(pid)
    if not models:
        pytest.skip(f"{pid} unreachable or has no models: {env.svc.startup_status['providers'].get(pid)}")
    return models


async def infer(env, body, timeout=600):
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev), timeout=timeout) as c:
        return await c.post("/api/v1/inference", json=body)


async def stream(env, body, timeout=600):
    async with httpx.AsyncClient(base_url=env.http, headers=headers(env.keys.dev), timeout=timeout) as c:
        async with c.stream("POST", "/api/v1/inference", json={**body, "stream": True}) as r:
            return [json.loads(line[5:]) async for line in r.aiter_lines() if line.startswith("data:")]


def check_stream(evs):
    kinds = [e["type"] for e in evs]
    assert kinds[0] == "started" and kinds[-1] == "completed", kinds[-3:]
    text = "".join(e["token"]["text"] for e in evs if e["type"] == "token")
    assert text.strip() and text == evs[-1]["completed"]["content"]
    usage = next(e["usage"] for e in evs if e["type"] == "usage")
    return text, usage


async def test_ollama_sync_and_stream(live):
    assert OLLAMA_MODEL in catalog(live, "ollama")
    r = await infer(live, {"model": "local:default", "input": "Reply with exactly: pong",
                           "params": {"max_tokens": 20, "temperature": 0}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "pong" in body["content"].lower() and body["provider"] == "ollama"
    assert body["usage"]["estimated"] is False and body["usage"]["output_tokens"] > 0
    text, usage = check_stream(await stream(live, {"model": f"local:ollama/{OLLAMA_MODEL}",
                                                   "input": "Count from 1 to 5.", "params": {"max_tokens": 60}}))
    assert "3" in text and usage["estimated"] is False


async def test_ollama_structured_output(live):
    catalog(live, "ollama")
    schema = {"type": "object", "required": ["city", "country"],
              "properties": {"city": {"type": "string"}, "country": {"type": "string"}}}
    r = await infer(live, {"model": "local:default", "input": "What is the capital of France? Answer as JSON.",
                           "params": {"response_schema": schema, "max_tokens": 80, "temperature": 0}})
    assert r.status_code == 200, r.text
    data = json.loads(r.json()["content"])
    assert data["city"].lower() == "paris"


async def test_llamacpp_sync_and_stream(live):
    if not os.environ.get("UAR_LLAMACPP_KEY"):
        pytest.skip("llama-server requires an API key: set UAR_LLAMACPP_KEY to run this gate")
    models = catalog(live, "llamacpp")
    model = os.environ.get("UAR_LIVE_LLAMACPP_MODEL") or next(
        (m for m in sorted(models) if "gemma-4-E4B" in m), sorted(models)[0])
    r = await infer(live, {"model": f"local:llamacpp/{model}", "input": "Reply with one word: ready",
                           "params": {"max_tokens": 200, "temperature": 0}})
    assert r.status_code == 200, r.text
    assert r.json()["provider"] == "llamacpp" and r.json()["usage"]["output_tokens"] > 0
    text, _ = check_stream(await stream(live, {"model": f"local:llamacpp/{model}", "input": "Name two colors.",
                                               "params": {"max_tokens": 200}}))
    assert text.strip()


async def test_lmstudio_sync_and_stream(live):
    models = catalog(live, "lmstudio")
    if LMSTUDIO_MODEL not in models:
        pytest.skip(f"{LMSTUDIO_MODEL} not in LM Studio catalog")
    ext = {"lmstudio": {"ttl": 120}}  # JIT-loaded model unloads after 2 idle minutes
    r = await infer(live, {"model": f"local:lmstudio/{LMSTUDIO_MODEL}", "input": "Reply with one word: ready",
                           "params": {"max_tokens": 1500, "temperature": 0}, "extensions": ext})
    assert r.status_code == 200, r.text
    assert r.json()["provider"] == "lmstudio" and r.json()["content"].strip()
    text, _ = check_stream(await stream(live, {"model": f"local:lmstudio/{LMSTUDIO_MODEL}",
                                               "input": "Name two colors.", "params": {"max_tokens": 1500},
                                               "extensions": ext}))
    assert text.strip()


async def test_ollama_auto_tool_use(live):
    catalog(live, "ollama")
    r = await infer(live, {"model": "local:default", "tool_mode": "auto", "tools": ["fs.read_text"],
                           "params": {"temperature": 0, "max_tokens": 200},
                           "messages": [{"role": "system", "content": "Use tools to answer. Files are relative paths."},
                                        {"role": "user", "content": "Read docs/product-faq.md and tell me how many "
                                                                    "days customers have to return items."}]})
    assert r.status_code == 200, r.text
    assert "30" in r.json()["content"], r.json()["content"]
    rows = await live.svc.store.fetchall("SELECT outcome FROM audit WHERE request_id=%s AND action='tool.execute'",
                                         r.headers["x-request-id"])
    assert [x["outcome"] for x in rows] == ["intent", "completed"]


async def test_report_generator_with_real_model(live):
    catalog(live, "ollama")
    async with httpx.AsyncClient(base_url=live.http, headers=headers(live.keys.dev), timeout=600) as c:
        r = await c.post("/api/v1/inference", json={"model": "local:default", "agent": "report_generator",
                                                    "input": "unused"})
        assert r.status_code == 400  # report_generator's input schema needs report_name, not prompt
        run = (await c.post("/api/v1/agent/run", json={"agent_id": "report_generator",
                                                       "input": {"report_name": "live_q2", "quarter": "2026-Q2"}})).json()
        import asyncio
        for _ in range(1200):
            run = (await c.get(f"/api/v1/runs/{run['run_id']}")).json()
            if run["status"] in ("succeeded", "failed", "needs_attention", "cancelled"):
                break
            await asyncio.sleep(0.25)
    assert run["status"] == "succeeded", run
    report = (live.ws / "reports" / "live_q2.md").read_text(encoding="utf-8")
    assert report.startswith("# ") and len(report) > 80
    assert run["usage"]["output_tokens"] > 0 and run["usage"]["estimated"] is False
    print("\n--- generated report ---\n" + report)
