"""Deterministic provider for CI, conformance fixtures and offline demos.

Behaviour (per last user message):
- default: replies "echo: <text>" in 3 streamed chunks.
- a response_schema: returns a minimal JSON object satisfying required string/number/array fields,
  or the object given in extension {"reply_json": {...}}.
- tools offered and no tool result yet: calls the first tool with args from extension {"tool_args": {...}}
  (or {} if absent) when the user text contains "use tool".
- extension {"fail": "unavailable"|"error"} simulates provider failures.
- extension {"delay_s": x} sleeps before answering; {"chunks": [..]} overrides stream chunks.
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from ...config import ProviderCfg
from ...errors import UARError
from .base import Adapter, ChatRequest, ChatResult, ProviderUnavailable, estimate_messages, estimate_tokens


def _fill(schema: dict) -> object:
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        props = schema.get("properties", {})
        return {k: _fill(props.get(k, {})) for k in schema.get("required", list(props))}
    if t == "array":
        return [_fill(schema.get("items", {}))] if schema.get("minItems") else []
    if t in ("number", "integer"):
        return 1
    if t == "boolean":
        return True
    if "enum" in schema:
        return schema["enum"][0]
    return "fake"


class FakeAdapter(Adapter):
    calls = 0  # class-level counter, used by tests to prove non-execution

    def __init__(self, cfg: ProviderCfg):
        self.id = cfg.id
        self.cfg = cfg
        self.capabilities = set(cfg.capabilities)

    def _answer(self, req: ChatRequest) -> ChatResult:
        FakeAdapter.calls += 1
        ext = req.extensions
        fail = ext.get("fail")
        if fail == "unavailable":
            raise ProviderUnavailable(self.id, "simulated")
        if fail:
            raise UARError("provider_error", f"{self.id}: simulated failure")
        last_user = next((m.get("content") or "" for m in reversed(req.messages) if m["role"] == "user"), "")
        has_tool_result = any(m["role"] == "tool" for m in req.messages)
        if req.tools and "use tool" in last_user.lower() and not has_tool_result:
            tool = req.tools[0]
            tc = {"id": "call_0", "name": tool.name, "args": ext.get("tool_args", {})}
            return ChatResult("", [tc], "tool_calls", estimate_messages(req.messages), 5, req.model)
        if req.response_schema:
            content = json.dumps(ext.get("reply_json") or _fill(req.response_schema))
        elif has_tool_result:
            tool_text = next(m.get("content") or "" for m in reversed(req.messages) if m["role"] == "tool")
            content = f"tool said: {tool_text[:200]}"
        else:
            content = ext.get("reply") or f"echo: {last_user}"
        if req.max_tokens is not None and estimate_tokens(content) > req.max_tokens:
            content = content[: req.max_tokens * 4]
            return ChatResult(content, [], "length", estimate_messages(req.messages), req.max_tokens, req.model)
        return ChatResult(content, [], "stop", estimate_messages(req.messages), estimate_tokens(content), req.model)

    async def chat(self, req: ChatRequest) -> ChatResult:
        await asyncio.sleep(float(req.extensions.get("delay_s", 0)))
        return self._answer(req)

    async def stream(self, req: ChatRequest) -> AsyncIterator[str | ChatResult]:
        res = self._answer(req)
        chunks = req.extensions.get("chunks")
        if not chunks:
            c = res.content
            n = max(1, len(c) // 3)
            chunks = [c[i:i + n] for i in range(0, len(c), n)] if c else []
        for ch in chunks:
            await asyncio.sleep(float(req.extensions.get("delay_s", 0)))
            yield ch
        res.content = "".join(chunks)
        yield res

    async def list_models(self) -> list[str]:
        return list(self.cfg.models) or ["echo", "echo-large"]
