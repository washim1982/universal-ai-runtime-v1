"""Ollama native API (/api/chat, /api/tags)."""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from ...config import ProviderCfg
from .base import (Adapter, ChatRequest, ChatResult, connect_error, http_error, parse_json_args, unwire_name,
                   wire_name)


class OllamaAdapter(Adapter):
    def __init__(self, cfg: ProviderCfg):
        self.id = cfg.id
        self.cfg = cfg
        self.capabilities = set(cfg.capabilities)
        self.http = httpx.AsyncClient(base_url=cfg.base_url or "http://127.0.0.1:11434",
                                      timeout=httpx.Timeout(cfg.timeout_s, connect=cfg.connect_timeout_s))

    def _body(self, req: ChatRequest, stream: bool) -> dict:
        msgs = []
        for m in req.messages:
            out = {"role": m["role"], "content": m.get("content") or ""}
            if m.get("tool_calls"):
                out["tool_calls"] = [{"function": {"name": wire_name(tc["name"]), "arguments": tc.get("args") or {}}}
                                     for tc in m["tool_calls"]]
            if m["role"] == "tool" and m.get("name"):
                out["tool_name"] = wire_name(m["name"])
            msgs.append(out)
        options = {k: v for k, v in {"temperature": req.temperature, "num_predict": req.max_tokens,
                                     "top_p": req.top_p, "stop": req.stop or None}.items() if v is not None}
        body: dict = {"model": req.model, "messages": msgs, "stream": stream, "options": options}
        if req.tools:
            body["tools"] = [{"type": "function", "function": {"name": wire_name(t.name), "description": t.description,
                                                               "parameters": t.input_schema}} for t in req.tools]
        if req.response_schema:
            body["format"] = req.response_schema
        ext = req.extensions
        if "keep_alive" in ext:
            body["keep_alive"] = ext["keep_alive"]
        if "think" in ext:
            body["think"] = ext["think"]
        return body

    @staticmethod
    def _tool_calls(msg: dict) -> list[dict]:
        return [{"id": f"call_{i}", "name": unwire_name(tc["function"]["name"]),
                 "args": parse_json_args(tc["function"].get("arguments"))}
                for i, tc in enumerate(msg.get("tool_calls") or [])]

    @staticmethod
    def _finish(d: dict, tool_calls: list) -> str:
        if tool_calls:
            return "tool_calls"
        return "length" if d.get("done_reason") == "length" else "stop"

    async def chat(self, req: ChatRequest) -> ChatResult:
        try:
            r = await self.http.post("/api/chat", json=self._body(req, False))
        except httpx.HTTPError as e:
            raise connect_error(self.id, e) from e
        if r.status_code != 200:
            raise http_error(self.id, r)
        d = r.json()
        msg = d.get("message") or {}
        tcs = self._tool_calls(msg)
        return ChatResult(msg.get("content") or "", tcs, self._finish(d, tcs),
                          d.get("prompt_eval_count"), d.get("eval_count"), d.get("model", req.model))

    async def stream(self, req: ChatRequest) -> AsyncIterator[str | ChatResult]:
        content, tcs, last = [], [], {}
        try:
            async with self.http.stream("POST", "/api/chat", json=self._body(req, True)) as r:
                if r.status_code != 200:
                    await r.aread()
                    raise http_error(self.id, r)
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    d = json.loads(line)
                    if d.get("error"):
                        raise http_error(self.id, httpx.Response(500, text=str(d["error"])))
                    msg = d.get("message") or {}
                    if msg.get("content"):
                        content.append(msg["content"])
                        yield msg["content"]
                    if msg.get("tool_calls"):
                        tcs.extend(self._tool_calls(msg))
                    if d.get("done"):
                        last = d
        except httpx.HTTPError as e:
            raise connect_error(self.id, e) from e
        for i, tc in enumerate(tcs):
            tc["id"] = f"call_{i}"
        yield ChatResult("".join(content), tcs, self._finish(last, tcs), last.get("prompt_eval_count"),
                         last.get("eval_count"), last.get("model", req.model))

    async def list_models(self) -> list[str]:
        try:
            r = await self.http.get("/api/tags", timeout=5)
        except httpx.HTTPError as e:
            raise connect_error(self.id, e) from e
        if r.status_code != 200:
            raise http_error(self.id, r)
        return [m["name"] for m in r.json().get("models", [])]

    async def aclose(self) -> None:
        await self.http.aclose()
