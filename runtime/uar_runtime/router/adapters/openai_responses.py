"""OpenAI Responses API (/v1/responses). Contract-tested with recorded fixtures; live use is opt-in."""
from __future__ import annotations

import json
import os
from typing import AsyncIterator

import httpx

from ...config import ProviderCfg
from ...errors import UARError
from .base import (Adapter, ChatRequest, ChatResult, connect_error, http_error, iter_sse, parse_json_args,
                   unwire_name, wire_name)


class OpenAIResponsesAdapter(Adapter):
    def __init__(self, cfg: ProviderCfg):
        self.id = cfg.id
        self.cfg = cfg
        self.capabilities = set(cfg.capabilities)
        key = os.environ.get(cfg.api_key_env or "OPENAI_API_KEY")
        headers = dict(cfg.extra_headers)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        self._has_key = bool(key)
        self.http = httpx.AsyncClient(base_url=(cfg.base_url or "https://api.openai.com/v1").rstrip("/"),
                                      headers=headers, follow_redirects=False,
                                      timeout=httpx.Timeout(cfg.timeout_s, connect=cfg.connect_timeout_s))

    def _body(self, req: ChatRequest, stream: bool) -> dict:
        instructions, items = [], []
        for m in req.messages:
            role = m["role"]
            if role == "system":
                instructions.append(m.get("content") or "")
            elif role == "tool":
                items.append({"type": "function_call_output", "call_id": m.get("tool_call_id", ""),
                              "output": m.get("content") or ""})
            else:
                if m.get("content"):
                    items.append({"role": role, "content": m["content"]})
                for tc in m.get("tool_calls") or []:
                    items.append({"type": "function_call", "call_id": tc["id"], "name": wire_name(tc["name"]),
                                  "arguments": json.dumps(tc.get("args") or {})})
        body: dict = {"model": req.model, "input": items, "stream": stream, "store": False}
        if instructions:
            body["instructions"] = "\n\n".join(instructions)
        if req.max_tokens is not None:
            body["max_output_tokens"] = req.max_tokens
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.top_p is not None:
            body["top_p"] = req.top_p
        if req.tools:
            body["tools"] = [{"type": "function", "name": wire_name(t.name), "description": t.description,
                              "parameters": t.input_schema} for t in req.tools]
        if req.response_schema:
            body["text"] = {"format": {"type": "json_schema", "name": "output", "schema": req.response_schema}}
        body.update({k: v for k, v in req.extensions.items() if k not in body})
        return body

    @staticmethod
    def _parse(d: dict, model: str) -> ChatResult:
        text, tcs = [], []
        for item in d.get("output") or []:
            if item.get("type") == "message":
                for c in item.get("content") or []:
                    if c.get("type") == "output_text":
                        text.append(c.get("text", ""))
                    elif c.get("type") == "refusal":
                        return ChatResult(c.get("refusal", ""), [], "refusal")
            elif item.get("type") == "function_call":
                tcs.append({"id": item.get("call_id") or item.get("id", ""), "name": unwire_name(item["name"]),
                            "args": parse_json_args(item.get("arguments"))})
        usage = d.get("usage") or {}
        finish = "tool_calls" if tcs else "stop"
        if d.get("status") == "incomplete" and (d.get("incomplete_details") or {}).get("reason") == "max_output_tokens":
            finish = "length"
        return ChatResult("".join(text), tcs, finish, usage.get("input_tokens"), usage.get("output_tokens"),
                          d.get("model", model))

    def _require_key(self) -> None:
        if not self._has_key:
            raise UARError("unavailable", f"{self.id}: credential {self.cfg.api_key_env or 'OPENAI_API_KEY'} "
                           "is not configured", retryable=False)

    async def chat(self, req: ChatRequest) -> ChatResult:
        self._require_key()
        try:
            r = await self.http.post("/responses", json=self._body(req, False))
        except httpx.HTTPError as e:
            raise connect_error(self.id, e) from e
        if r.status_code != 200:
            raise http_error(self.id, r)
        return self._parse(r.json(), req.model)

    async def stream(self, req: ChatRequest) -> AsyncIterator[str | ChatResult]:
        self._require_key()
        final = None
        try:
            async with self.http.stream("POST", "/responses", json=self._body(req, True)) as r:
                if r.status_code != 200:
                    await r.aread()
                    raise http_error(self.id, r)
                async for event, data in iter_sse(r):
                    d = json.loads(data)
                    kind = d.get("type", event)
                    if kind == "response.output_text.delta":
                        yield d.get("delta", "")
                    elif kind in ("response.completed", "response.incomplete"):
                        final = self._parse(d.get("response") or {}, req.model)
                    elif kind in ("response.failed", "error"):
                        raise UARError("provider_error", f"{self.id}: stream failed",
                                       details={"error": d.get("error") or (d.get("response") or {}).get("error")})
        except httpx.HTTPError as e:
            raise connect_error(self.id, e) from e
        if final is None:
            raise UARError("provider_error", f"{self.id}: stream ended without a final response")
        yield final

    async def list_models(self) -> list[str]:
        return list(self.cfg.models)

    async def aclose(self) -> None:
        await self.http.aclose()
