"""OpenAI-compatible Chat Completions (/v1/chat/completions).

Serves LM Studio, llama.cpp llama-server, vLLM, Groq and most enterprise gateways.
"""
from __future__ import annotations

import json
import os
from typing import AsyncIterator

import httpx

from ...config import ProviderCfg
from ...errors import UARError
from .base import (Adapter, ChatRequest, ChatResult, connect_error, http_error, iter_sse, parse_json_args,
                   unwire_name, wire_name)

_FINISH = {"stop": "stop", "length": "length", "tool_calls": "tool_calls", "function_call": "tool_calls",
           "content_filter": "refusal"}


class OpenAICompatAdapter(Adapter):
    def __init__(self, cfg: ProviderCfg):
        self.id = cfg.id
        self.cfg = cfg
        self.capabilities = set(cfg.capabilities)
        headers = dict(cfg.extra_headers)
        if cfg.api_key_env:
            key = os.environ.get(cfg.api_key_env)
            if key:
                headers["Authorization"] = f"Bearer {key}"
        base = (cfg.base_url or "http://127.0.0.1:1234/v1").rstrip("/")
        self.http = httpx.AsyncClient(base_url=base, headers=headers, follow_redirects=False,
                                      timeout=httpx.Timeout(cfg.timeout_s, connect=cfg.connect_timeout_s))
        self._has_key = "Authorization" in headers

    def _check_key(self) -> None:
        # Cloud endpoints always need their key; a local/enterprise server may run without one.
        if self.cfg.api_key_env and not self._has_key and self.cfg.model_class == "cloud":
            raise UARError("unavailable", f"{self.id}: credential {self.cfg.api_key_env} is not configured",
                           retryable=False)

    def _body(self, req: ChatRequest, stream: bool) -> dict:
        msgs = []
        for m in req.messages:
            out: dict = {"role": m["role"], "content": m.get("content") or ""}
            if m.get("tool_calls"):
                out["tool_calls"] = [{"id": tc["id"], "type": "function",
                                      "function": {"name": wire_name(tc["name"]),
                                                   "arguments": json.dumps(tc.get("args") or {})}}
                                     for tc in m["tool_calls"]]
            if m["role"] == "tool":
                out["tool_call_id"] = m.get("tool_call_id", "")
            msgs.append(out)
        body: dict = {"model": req.model, "messages": msgs, "stream": stream}
        for k, v in (("temperature", req.temperature), ("max_tokens", req.max_tokens), ("top_p", req.top_p)):
            if v is not None:
                body[k] = v
        if req.stop:
            body["stop"] = req.stop
        if req.tools:
            body["tools"] = [{"type": "function", "function": {"name": wire_name(t.name), "description": t.description,
                                                               "parameters": t.input_schema}} for t in req.tools]
        if req.response_schema:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "output", "schema": req.response_schema}}
        if stream:
            body["stream_options"] = {"include_usage": True}
        body.update({k: v for k, v in req.extensions.items() if k not in body})
        return body

    async def chat(self, req: ChatRequest) -> ChatResult:
        self._check_key()
        try:
            r = await self.http.post("/chat/completions", json=self._body(req, False))
        except httpx.HTTPError as e:
            raise connect_error(self.id, e) from e
        if r.status_code != 200:
            raise http_error(self.id, r)
        d = r.json()
        choice = (d.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tcs = [{"id": tc.get("id") or f"call_{i}", "name": unwire_name(tc["function"]["name"]),
                "args": parse_json_args(tc["function"].get("arguments"))}
               for i, tc in enumerate(msg.get("tool_calls") or [])]
        usage = d.get("usage") or {}
        finish = "tool_calls" if tcs else _FINISH.get(choice.get("finish_reason") or "stop", "stop")
        return ChatResult(msg.get("content") or "", tcs, finish, usage.get("prompt_tokens"),
                          usage.get("completion_tokens"), d.get("model", req.model))

    async def stream(self, req: ChatRequest) -> AsyncIterator[str | ChatResult]:
        self._check_key()
        content: list[str] = []
        calls: dict[int, dict] = {}
        finish, usage, model = "stop", {}, req.model
        try:
            async with self.http.stream("POST", "/chat/completions", json=self._body(req, True)) as r:
                if r.status_code != 200:
                    await r.aread()
                    raise http_error(self.id, r)
                async for _, data in iter_sse(r):
                    if data.strip() == "[DONE]":
                        break
                    d = json.loads(data)
                    if d.get("error"):
                        raise UARError("provider_error", f"{self.id}: stream error", details={"error": d["error"]})
                    model = d.get("model") or model
                    if d.get("usage"):
                        usage = d["usage"]
                    for choice in d.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            content.append(delta["content"])
                            yield delta["content"]
                        for tc in delta.get("tool_calls") or []:
                            slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                            slot["id"] = tc.get("id") or slot["id"]
                            fn = tc.get("function") or {}
                            slot["name"] += fn.get("name") or ""
                            slot["args"] += fn.get("arguments") or ""
                        if choice.get("finish_reason"):
                            finish = _FINISH.get(choice["finish_reason"], "stop")
        except httpx.HTTPError as e:
            raise connect_error(self.id, e) from e
        tcs = [{"id": c["id"] or f"call_{i}", "name": unwire_name(c["name"]), "args": parse_json_args(c["args"])}
               for i, c in sorted(calls.items())]
        yield ChatResult("".join(content), tcs, "tool_calls" if tcs else finish, usage.get("prompt_tokens"),
                         usage.get("completion_tokens"), model)

    async def list_models(self) -> list[str]:
        if self.cfg.api_key_env and not self._has_key and self.cfg.model_class == "cloud":
            return []
        try:
            r = await self.http.get("/models", timeout=5)
        except httpx.HTTPError as e:
            raise connect_error(self.id, e) from e
        if r.status_code != 200:
            raise http_error(self.id, r)
        return [m["id"] for m in r.json().get("data", [])]

    async def aclose(self) -> None:
        await self.http.aclose()
