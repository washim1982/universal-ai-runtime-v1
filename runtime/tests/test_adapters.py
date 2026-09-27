"""Provider adapter contract tests against a local fake server that replays each API's documented
wire format (JSON and SSE). No credentials or network access are used. The fake also records what
the adapter sent, so request shaping (tools, schemas, system prompts, betas) is verified too."""
from __future__ import annotations

import asyncio
import json

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from uar_runtime.config import ProviderCfg
from uar_runtime.errors import UARError
from uar_runtime.router.adapters.anthropic_messages import AnthropicAdapter
from uar_runtime.router.adapters.base import ChatRequest, ChatResult, ProviderUnavailable, ToolSpec
from uar_runtime.router.adapters.ollama import OllamaAdapter
from uar_runtime.router.adapters.openai_compat import OpenAICompatAdapter
from uar_runtime.router.adapters.openai_responses import OpenAIResponsesAdapter

TOOL = ToolSpec("fs.read_text", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}},
                                               "required": ["path"]})


def sse(events: list[tuple[str, dict]]) -> bytes:
    return b"".join(f"event: {e}\ndata: {json.dumps(d)}\n\n".encode() for e, d in events)


class Fake:
    def __init__(self):
        self.requests: list[dict] = []
        self.next: list[Response] = []

    async def handle(self, request: Request) -> Response:
        body = await request.body()
        self.requests.append({"path": request.url.path, "query": str(request.url.query),
                              "headers": dict(request.headers), "json": json.loads(body) if body else None})
        return self.next.pop(0)


@pytest.fixture(scope="module")
async def fake():
    f = Fake()
    app = Starlette(routes=[Route("/{p:path}", f.handle, methods=["GET", "POST"])])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, lifespan="off"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    f.url = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    yield f
    server.should_exit = True
    await task


def cfg(pid, type_, url, cls="cloud", key_env=None):
    return ProviderCfg(id=pid, type=type_, model_class=cls, base_url=url, api_key_env=key_env, timeout_s=10)


async def collect(gen) -> tuple[list[str], ChatResult]:
    texts, final = [], None
    async for item in gen:
        if isinstance(item, ChatResult):
            final = item
        else:
            texts.append(item)
    return texts, final


# ------------------------------------------------------------------ Anthropic (official SDK)

@pytest.fixture
def anthropic_key(monkeypatch):
    monkeypatch.setenv("UAR_TEST_ANTHROPIC_KEY", "sk-ant-test-not-real")


async def test_anthropic_sync_request_shape_and_tool_use(fake, anthropic_key):
    a = AnthropicAdapter(cfg("anthropic", "anthropic", fake.url, key_env="UAR_TEST_ANTHROPIC_KEY"))
    fake.next.append(JSONResponse({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5",
        "content": [{"type": "text", "text": "Let me read it."},
                    {"type": "tool_use", "id": "toolu_1", "name": "fs__read_text", "input": {"path": "a.md"}}],
        "stop_reason": "tool_use", "stop_sequence": None, "usage": {"input_tokens": 21, "output_tokens": 9}}))
    res = await a.chat(ChatRequest(model="claude-opus-5", tools=[TOOL], messages=[
        {"role": "system", "content": "Be brief."}, {"role": "user", "content": "Read a.md"}],
        temperature=0.3, response_schema=None))
    assert res.finish_reason == "tool_calls" and res.content == "Let me read it."
    assert res.tool_calls == [{"id": "toolu_1", "name": "fs.read_text", "args": {"path": "a.md"}}]
    assert (res.input_tokens, res.output_tokens) == (21, 9)
    sent = fake.requests[-1]
    assert sent["path"] == "/v1/messages" and sent["headers"]["x-api-key"] == "sk-ant-test-not-real"
    assert "server-side-fallback-2026-07-01" in sent["headers"]["anthropic-beta"]
    body = sent["json"]
    assert body["system"] == "Be brief." and body["fallbacks"] == "default" and body["max_tokens"] == 16000
    assert body["tools"][0]["name"] == "fs__read_text" and "eager_input_streaming" not in body["tools"][0]
    assert "temperature" not in body, "sampling parameters are not sent to current Claude models"
    await a.aclose()


async def test_anthropic_tool_results_grouped_and_schema(fake, anthropic_key):
    a = AnthropicAdapter(cfg("anthropic", "anthropic", fake.url, key_env="UAR_TEST_ANTHROPIC_KEY"))
    fake.next.append(JSONResponse({
        "id": "msg_2", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
        "content": [{"type": "text", "text": "{\"ok\": true}"}], "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 40, "output_tokens": 4}}))
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "name": "fs.read_text", "args": {}},
                                                                {"id": "t2", "name": "fs.read_text", "args": {}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "one"},
            {"role": "tool", "tool_call_id": "t2", "content": "two", "is_error": True}]
    res = await a.chat(ChatRequest(model="claude-sonnet-5", messages=msgs, response_schema={"type": "object"}))
    assert res.content == '{"ok": true}' and res.finish_reason == "stop"
    body = fake.requests[-1]["json"]
    assert "fallbacks" not in body, "fallbacks only for models that support them"
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": {"type": "object"}}}
    results = body["messages"][2]["content"]
    assert [b["tool_use_id"] for b in results] == ["t1", "t2"] and results[1]["is_error"] is True, \
        "all tool results for one turn go in a single user message"
    await a.aclose()


async def test_anthropic_stream_and_refusal(fake, anthropic_key):
    a = AnthropicAdapter(cfg("anthropic", "anthropic", fake.url, key_env="UAR_TEST_ANTHROPIC_KEY"))
    msg = {"id": "msg_3", "type": "message", "role": "assistant", "model": "claude-opus-5", "content": [],
           "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 1}}
    fake.next.append(Response(sse([
        ("message_start", {"type": "message_start", "message": msg}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "Hel"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "lo"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 5}}),
        ("message_stop", {"type": "message_stop"})]), media_type="text/event-stream"))
    texts, final = await collect(a.stream(ChatRequest(model="claude-opus-5",
                                                      messages=[{"role": "user", "content": "hi"}], tools=[TOOL])))
    assert texts == ["Hel", "lo"] and final.content == "Hello" and final.finish_reason == "stop"
    assert final.output_tokens == 5
    body = fake.requests[-1]["json"]
    assert body["stream"] is True and body["max_tokens"] == 64000 and body["tools"][0]["eager_input_streaming"]
    fake.next.append(JSONResponse({
        "id": "msg_4", "type": "message", "role": "assistant", "model": "claude-opus-5",
        "content": [{"type": "tool_use", "id": "toolu_9", "name": "fs__read_text", "input": {"path": "x"}}],
        "stop_reason": "refusal", "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 2}}))
    res = await a.chat(ChatRequest(model="claude-opus-5", messages=[{"role": "user", "content": "x"}]))
    assert res.finish_reason == "refusal" and res.tool_calls == [], "refused tool input must never run"
    await a.aclose()


async def test_anthropic_errors_and_missing_key(fake, anthropic_key, monkeypatch):
    a = AnthropicAdapter(cfg("anthropic", "anthropic", fake.url, key_env="UAR_TEST_ANTHROPIC_KEY"))
    for status, code in ((429, "rate_limited"), (404, "not_found"), (400, "provider_error"), (401, "provider_error")):
        for _ in range(2):  # the SDK retries 429/5xx once (max_retries=1)
            fake.next.append(JSONResponse({"type": "error", "error": {"type": "x", "message": "m"}}, status_code=status))
        with pytest.raises(UARError) as ei:
            await a.chat(ChatRequest(model="claude-opus-5", messages=[{"role": "user", "content": "x"}]))
        assert ei.value.code == code, status
        fake.next.clear()
    await a.aclose()
    monkeypatch.delenv("UAR_TEST_ANTHROPIC_KEY")
    with pytest.raises(UARError) as ei:
        await AnthropicAdapter(cfg("anthropic", "anthropic", fake.url, key_env="UAR_TEST_ANTHROPIC_KEY")).chat(
            ChatRequest(model="m", messages=[{"role": "user", "content": "x"}]))
    assert ei.value.code == "unavailable" and not ei.value.retryable
    down = AnthropicAdapter(cfg("anthropic", "anthropic", "http://127.0.0.1:9", key_env="PATH"))
    with pytest.raises(ProviderUnavailable):
        await down.chat(ChatRequest(model="m", messages=[{"role": "user", "content": "x"}]))


# ------------------------------------------------------------------ OpenAI Responses

@pytest.fixture
def openai_key(monkeypatch):
    monkeypatch.setenv("UAR_TEST_OPENAI_KEY", "sk-test-not-real")


async def test_openai_responses_sync_and_function_call(fake, openai_key):
    a = OpenAIResponsesAdapter(cfg("openai", "openai", fake.url + "/v1", key_env="UAR_TEST_OPENAI_KEY"))
    fake.next.append(JSONResponse({
        "id": "resp_1", "object": "response", "status": "completed", "model": "gpt-test",
        "output": [{"type": "function_call", "call_id": "call_1", "name": "fs__read_text",
                    "arguments": "{\"path\": \"a.md\"}"}],
        "usage": {"input_tokens": 30, "output_tokens": 7}}))
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c0", "name": "fs.list_dir", "args": {}}]},
            {"role": "tool", "tool_call_id": "c0", "content": "[]"}]
    res = await a.chat(ChatRequest(model="gpt-test", messages=msgs, tools=[TOOL], max_tokens=50,
                                   response_schema={"type": "object"}))
    assert res.tool_calls == [{"id": "call_1", "name": "fs.read_text", "args": {"path": "a.md"}}]
    assert res.finish_reason == "tool_calls" and (res.input_tokens, res.output_tokens) == (30, 7)
    sent = fake.requests[-1]
    assert sent["path"] == "/v1/responses" and sent["headers"]["authorization"] == "Bearer sk-test-not-real"
    body = sent["json"]
    assert body["instructions"] == "sys" and body["max_output_tokens"] == 50 and body["store"] is False
    assert [i.get("type", "message") for i in body["input"]] == ["message", "function_call", "function_call_output"]
    assert body["tools"][0] == {"type": "function", "name": "fs__read_text", "description": "Read a file",
                                "parameters": TOOL.input_schema}
    assert body["text"]["format"]["type"] == "json_schema"
    await a.aclose()


async def test_openai_responses_stream_incomplete_and_errors(fake, openai_key):
    a = OpenAIResponsesAdapter(cfg("openai", "openai", fake.url + "/v1", key_env="UAR_TEST_OPENAI_KEY"))
    final = {"id": "resp_2", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
             "model": "gpt-test", "output": [{"type": "message", "role": "assistant",
                                              "content": [{"type": "output_text", "text": "Hel"}]}],
             "usage": {"input_tokens": 4, "output_tokens": 2}}
    fake.next.append(Response(sse([
        ("response.created", {"type": "response.created", "response": {"id": "resp_2"}}),
        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "He"}),
        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "l"}),
        ("response.incomplete", {"type": "response.incomplete", "response": final})]),
        media_type="text/event-stream"))
    texts, res = await collect(a.stream(ChatRequest(model="gpt-test", messages=[{"role": "user", "content": "x"}])))
    assert texts == ["He", "l"] and res.finish_reason == "length" and res.output_tokens == 2
    fake.next.append(JSONResponse({"error": {"message": "slow down"}}, status_code=429))
    with pytest.raises(UARError) as ei:
        await a.chat(ChatRequest(model="gpt-test", messages=[{"role": "user", "content": "x"}]))
    assert ei.value.code == "rate_limited"
    await a.aclose()


# ------------------------------------------------------------------ OpenAI-compatible (Groq, vLLM, ...)

async def test_openai_compat_stream_with_tool_call_deltas(fake, monkeypatch):
    monkeypatch.setenv("UAR_TEST_GROQ_KEY", "gsk-test")
    a = OpenAICompatAdapter(cfg("groq", "openai_compat", fake.url + "/openai/v1", key_env="UAR_TEST_GROQ_KEY"))
    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Checking"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
                                                            "function": {"name": "fs__read_text", "arguments": ""}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"path\":"}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"a.md\"}"}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 6}},
    ]
    body = b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"
    fake.next.append(Response(body, media_type="text/event-stream"))
    texts, res = await collect(a.stream(ChatRequest(model="llama-x", tools=[TOOL],
                                                   messages=[{"role": "user", "content": "q"}])))
    assert texts == ["Checking"] and res.finish_reason == "tool_calls"
    assert res.tool_calls == [{"id": "call_a", "name": "fs.read_text", "args": {"path": "a.md"}}]
    assert (res.input_tokens, res.output_tokens) == (11, 6)
    sent = fake.requests[-1]
    assert sent["headers"]["authorization"] == "Bearer gsk-test" and sent["json"]["stream_options"]["include_usage"]
    await a.aclose()


async def test_openai_compat_cloud_requires_key_local_does_not(fake, monkeypatch):
    monkeypatch.delenv("UAR_TEST_GROQ_KEY", raising=False)
    cloud = OpenAICompatAdapter(cfg("groq", "openai_compat", fake.url + "/v1", key_env="UAR_TEST_GROQ_KEY"))
    with pytest.raises(UARError) as ei:
        await cloud.chat(ChatRequest(model="m", messages=[{"role": "user", "content": "x"}]))
    assert ei.value.code == "unavailable"
    local = OpenAICompatAdapter(cfg("llamacpp", "openai_compat", fake.url + "/v1", cls="local",
                                    key_env="UAR_TEST_GROQ_KEY"))
    fake.next.append(JSONResponse({"model": "m", "choices": [{"message": {"content": "ok"}, "finish_reason": "length"}],
                                   "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
    res = await local.chat(ChatRequest(model="m", messages=[{"role": "user", "content": "x"}]))
    assert res.content == "ok" and res.finish_reason == "length"
    assert "authorization" not in fake.requests[-1]["headers"]


# ------------------------------------------------------------------ Ollama (native)

async def test_ollama_stream_tool_calls_and_options(fake):
    a = OllamaAdapter(cfg("ollama", "ollama", fake.url, cls="local"))
    lines = [{"model": "m", "message": {"role": "assistant", "content": "Hi"}, "done": False},
             {"model": "m", "message": {"role": "assistant", "content": "",
                                        "tool_calls": [{"function": {"name": "fs__read_text",
                                                                     "arguments": {"path": "a"}}}]}, "done": False},
             {"model": "m", "message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
              "prompt_eval_count": 9, "eval_count": 3}]
    fake.next.append(Response("\n".join(json.dumps(x) for x in lines) + "\n", media_type="application/x-ndjson"))
    texts, res = await collect(a.stream(ChatRequest(model="m", tools=[TOOL], max_tokens=20, temperature=0,
                                                   response_schema={"type": "object"},
                                                   messages=[{"role": "user", "content": "q"}],
                                                   extensions={"keep_alive": "5m"})))
    assert texts == ["Hi"] and res.finish_reason == "tool_calls" and res.tool_calls[0]["name"] == "fs.read_text"
    body = fake.requests[-1]["json"]
    assert body["options"] == {"temperature": 0, "num_predict": 20} and body["format"] == {"type": "object"}
    assert body["keep_alive"] == "5m" and body["tools"][0]["function"]["name"] == "fs__read_text"
    await a.aclose()
