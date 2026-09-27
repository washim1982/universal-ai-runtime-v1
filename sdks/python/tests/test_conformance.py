"""Python SDK conformance: every shared fixture through the SDK, against uar-mock (default) or a live
runtime (UAR_LIVE_URL + UAR_LIVE_KEY; the live runtime must use the test configuration)."""
from __future__ import annotations

import copy
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "mock"))
sys.path.insert(0, str(ROOT / "sdks" / "python"))

import uar  # noqa: E402
import uar_mock  # noqa: E402

FIXTURES = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted((ROOT / "contracts/fixtures").glob("*.json"))}


def strip(value, ignore):
    v = copy.deepcopy(value)
    for path in ignore:
        cur, parts = v, path.split(".")
        for p in parts[:-1]:
            cur = cur.get(p, {}) if isinstance(cur, dict) else {}
        if isinstance(cur, dict):
            cur.pop(parts[-1], None)
    return v


@pytest.fixture(scope="session")
def target():
    if os.environ.get("UAR_LIVE_URL"):
        yield os.environ["UAR_LIVE_URL"], os.environ["UAR_LIVE_KEY"]
        return
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(uar_mock.create_app(), host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}", "uar_mock0000_notasecretjustamockkey"
    server.should_exit = True


def expect(name, got):
    fx = FIXTURES[name]["response"]
    assert strip(dict(got), fx["ignore"]) == strip(fx["body"], fx["ignore"])


def test_inference_basic(target):
    url, key = target
    with uar.Client(url, key) as c:
        resp = c.inference(model="local:default", prompt="hello")
    expect("inference_basic", resp)
    assert resp.text == "echo: hello" and resp.route.provider == "fake"


def test_inference_stream(target):
    url, key = target
    with uar.Client(url, key) as c:
        events = list(c.stream(model="local:default", prompt="stream"))
    fx = FIXTURES["inference_stream"]["response"]
    got = [strip({k: v for k, v in e.items() if k != "type"}, fx["ignore"]) for e in events]
    assert got == [strip(e, fx["ignore"]) for e in fx["events"]]
    assert events[-1].type == "completed" and events[-1].body["content"] == "echo: stream"
    assert "".join(e.body["text"] for e in events if e.type == "token") == "echo: stream"


def test_error_unauthenticated(target):
    url, _ = target
    with uar.Client(url, api_key="") as c:
        with pytest.raises(uar.AuthenticationError) as ei:
            c.inference(model="local:default", prompt="hi")
    assert ei.value.code == "unauthenticated" and ei.value.status == 401 and not ei.value.retryable


def test_tool_execute_read(target):
    url, key = target
    with uar.Client(url, key) as c:
        res = c.execute_tool("fs.read_text", {"path": "docs/faq.md"})
    expect("tool_execute_read", res)
    assert res.structured.content.startswith("# FAQ") and res.untrusted is True


def test_tool_policy_denied(target):
    url, key = target
    with uar.Client(url, key) as c:
        with pytest.raises(uar.PermissionDeniedError) as ei:
            c.execute_tool("fs.write_text", {"path": "docs/x.md", "content": "x"})
    assert ei.value.code == "policy_denied"


def test_run_not_found(target):
    url, key = target
    with uar.Client(url, key) as c:
        with pytest.raises(uar.NotFoundError) as ei:
            c.get_run("run_does_not_exist")
    assert ei.value.code == "not_found"


def test_run_start(target):
    url, key = target
    with uar.Client(url, key) as c:
        run = c.run_agent("in_app_assistant", {"prompt": "What is the return window?"})
    expect("run_start", run)
    assert run.run_id.startswith("run_")


def test_dry_run_static(target):
    url, key = target
    with uar.Client(url, key) as c:
        rep = c.dry_run("in_app_assistant", mode="static")
    expect("dry_run_static", rep)
    assert rep.executed_nothing is True


def test_every_fixture_has_an_sdk_test():
    tested = {n[len("test_"):] for n in globals() if n.startswith("test_")}
    assert set(FIXTURES) <= tested, set(FIXTURES) - tested


def test_no_retry_without_idempotency_key(monkeypatch):
    calls = []

    class Flaky:
        status_code = 503
        headers = {}

        def json(self):
            return {"error": {"code": "unavailable", "message": "x", "retryable": True}}

    c = uar.Client("http://127.0.0.1:1", "k", max_retries=3)
    monkeypatch.setattr(c._http, "request", lambda *a, **k: calls.append(k.get("headers")) or Flaky())
    monkeypatch.setattr(c, "_delay", lambda *a: 0)
    with pytest.raises(uar.UnavailableError):
        c.inference(model="m", prompt="p")
    assert len(calls) == 1, "inference must not be retried automatically"
    calls.clear()
    with pytest.raises(uar.UnavailableError):
        c.execute_tool("t", {}, idempotency_key="k1")
    assert len(calls) == 4 and calls[0]["Idempotency-Key"] == "k1"
