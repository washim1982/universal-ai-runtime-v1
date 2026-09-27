"""Google Vertex AI (Gemini models) via the native generateContent API.

base_url: https://<location>-aiplatform.googleapis.com/v1/projects/<project>/locations/<location>/publishers/google/models
Auth: a bearer token from api_key_env (default VERTEX_ACCESS_TOKEN, e.g. `gcloud auth print-access-token`),
or Application Default Credentials when the optional `google-auth` package is installed.
Other Google surfaces (the Gemini Developer API, Vertex's OpenAI-compatible endpoint) are separate
and not covered by this adapter.
"""
from __future__ import annotations

import json
import os
from typing import AsyncIterator

import httpx

from ...config import ProviderCfg
from ...errors import UARError
from .base import (Adapter, ChatRequest, ChatResult, connect_error, http_error, iter_sse, unwire_name, wire_name)

_FINISH = {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "refusal", "RECITATION": "refusal",
           "PROHIBITED_CONTENT": "refusal", "BLOCKLIST": "refusal", "SPII": "refusal"}


class VertexAdapter(Adapter):
    def __init__(self, cfg: ProviderCfg):
        self.id = cfg.id
        self.cfg = cfg
        self.capabilities = set(cfg.capabilities)
        self.http = httpx.AsyncClient(base_url=cfg.base_url.rstrip("/"), follow_redirects=False,
                                      headers=dict(cfg.extra_headers),
                                      timeout=httpx.Timeout(cfg.timeout_s, connect=cfg.connect_timeout_s))

    def _token(self) -> str:
        tok = os.environ.get(self.cfg.api_key_env or "VERTEX_ACCESS_TOKEN")
        if tok:
            return tok
        try:  # optional: Application Default Credentials
            import google.auth
            import google.auth.transport.requests
            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            creds.refresh(google.auth.transport.requests.Request())
            return creds.token
        except Exception:
            raise UARError("unavailable", f"{self.id}: no Vertex AI credentials (set "
                           f"{self.cfg.api_key_env or 'VERTEX_ACCESS_TOKEN'} or configure ADC)", retryable=False) from None

    def _body(self, req: ChatRequest) -> dict:
        system, contents = [], []
        names: dict[str, str] = {}
        for m in req.messages:
            role = m["role"]
            if role == "system":
                system.append(m.get("content") or "")
            elif role == "tool":
                name = names.get(m.get("tool_call_id", ""), m.get("name", "tool"))
                part = {"functionResponse": {"name": wire_name(name), "response": {"content": m.get("content") or ""}}}
                if contents and contents[-1]["role"] == "user" and "functionResponse" in contents[-1]["parts"][0]:
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
            else:
                parts = [{"text": m["content"]}] if m.get("content") else []
                for tc in m.get("tool_calls") or []:
                    names[tc["id"]] = tc["name"]
                    parts.append({"functionCall": {"name": wire_name(tc["name"]), "args": tc.get("args") or {}}})
                contents.append({"role": "model" if role == "assistant" else "user", "parts": parts or [{"text": ""}]})
        body: dict = {"contents": contents}
        if system:
            body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system)}]}
        gen = {k: v for k, v in {"temperature": req.temperature, "maxOutputTokens": req.max_tokens,
                                 "topP": req.top_p, "stopSequences": req.stop or None}.items() if v is not None}
        if req.response_schema:
            gen["responseMimeType"] = "application/json"
            gen["responseSchema"] = req.response_schema
        if gen:
            body["generationConfig"] = gen
        if req.tools:
            body["tools"] = [{"functionDeclarations": [{"name": wire_name(t.name), "description": t.description,
                                                        "parameters": t.input_schema} for t in req.tools]}]
        body.update({k: v for k, v in req.extensions.items() if k not in body})
        return body

    @staticmethod
    def _parse(d: dict, model: str) -> ChatResult:
        cands = d.get("candidates") or []
        if not cands:
            if (d.get("promptFeedback") or {}).get("blockReason"):
                return ChatResult("", [], "refusal")
            raise UARError("provider_error", "vertex: response had no candidates")
        c = cands[0]
        text, tcs = [], []
        for i, part in enumerate((c.get("content") or {}).get("parts") or []):
            if "text" in part and not part.get("thought"):
                text.append(part["text"])
            if "functionCall" in part:
                fc = part["functionCall"]
                tcs.append({"id": f"call_{i}", "name": unwire_name(fc["name"]), "args": fc.get("args") or {}})
        usage = d.get("usageMetadata") or {}
        finish = "tool_calls" if tcs else _FINISH.get(c.get("finishReason") or "STOP", "stop")
        return ChatResult("".join(text), tcs, finish, usage.get("promptTokenCount"),
                          usage.get("candidatesTokenCount"), d.get("modelVersion", model))

    async def chat(self, req: ChatRequest) -> ChatResult:
        tok = self._token()
        try:
            r = await self.http.post(f"/{req.model}:generateContent", json=self._body(req),
                                     headers={"Authorization": f"Bearer {tok}"})
        except httpx.HTTPError as e:
            raise connect_error(self.id, e) from e
        if r.status_code != 200:
            raise http_error(self.id, r)
        return self._parse(r.json(), req.model)

    async def stream(self, req: ChatRequest) -> AsyncIterator[str | ChatResult]:
        tok = self._token()
        text, tcs, usage, finish, model = [], [], {}, "STOP", req.model
        try:
            async with self.http.stream("POST", f"/{req.model}:streamGenerateContent?alt=sse", json=self._body(req),
                                        headers={"Authorization": f"Bearer {tok}"}) as r:
                if r.status_code != 200:
                    await r.aread()
                    raise http_error(self.id, r)
                async for _, data in iter_sse(r):
                    d = json.loads(data)
                    model = d.get("modelVersion", model)
                    usage = d.get("usageMetadata") or usage
                    for c in d.get("candidates") or []:
                        finish = c.get("finishReason") or finish
                        for i, part in enumerate((c.get("content") or {}).get("parts") or []):
                            if part.get("text") and not part.get("thought"):
                                text.append(part["text"])
                                yield part["text"]
                            if "functionCall" in part:
                                fc = part["functionCall"]
                                tcs.append({"id": f"call_{len(tcs)}", "name": unwire_name(fc["name"]),
                                            "args": fc.get("args") or {}})
        except httpx.HTTPError as e:
            raise connect_error(self.id, e) from e
        yield ChatResult("".join(text), tcs, "tool_calls" if tcs else _FINISH.get(finish, "stop"),
                         usage.get("promptTokenCount"), usage.get("candidatesTokenCount"), model)

    async def list_models(self) -> list[str]:
        return list(self.cfg.models)

    async def aclose(self) -> None:
        await self.http.aclose()
