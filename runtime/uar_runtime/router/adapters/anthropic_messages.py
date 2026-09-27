"""Anthropic Messages API via the official `anthropic` SDK.

Contract-tested against a local fake server; live use needs ANTHROPIC_API_KEY (or the
configured api_key_env). Server-side refusal fallbacks are enabled by default for models
that support them; set the provider extension {"fallbacks": "off"} to disable.
"""
from __future__ import annotations

import os
from typing import Any, AsyncIterator

import anthropic

from ...config import ProviderCfg
from ...errors import UARError
from .base import Adapter, ChatRequest, ChatResult, ProviderUnavailable, unwire_name, wire_name

_FINISH = {"end_turn": "stop", "stop_sequence": "stop", "pause_turn": "stop", "max_tokens": "length",
           "tool_use": "tool_calls", "refusal": "refusal"}
FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicAdapter(Adapter):
    def __init__(self, cfg: ProviderCfg):
        self.id = cfg.id
        self.cfg = cfg
        self.capabilities = set(cfg.capabilities)
        self._client: anthropic.AsyncAnthropic | None = None

    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            key = os.environ.get(self.cfg.api_key_env or "ANTHROPIC_API_KEY")
            if not key:
                raise UARError("unavailable", f"{self.id}: credential {self.cfg.api_key_env or 'ANTHROPIC_API_KEY'} "
                               "is not configured", retryable=False)
            self._client = anthropic.AsyncAnthropic(api_key=key, base_url=self.cfg.base_url or None,
                                                    timeout=self.cfg.timeout_s, max_retries=1,
                                                    default_headers=self.cfg.extra_headers or None)
        return self._client

    def _params(self, req: ChatRequest, stream: bool) -> dict[str, Any]:
        system, msgs = [], []
        for m in req.messages:
            role = m["role"]
            if role == "system":
                system.append(m.get("content") or "")
                continue
            if role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""),
                         "content": m.get("content") or ""}
                if m.get("is_error"):
                    block["is_error"] = True
                # All results for one assistant turn go in a single user message.
                if msgs and msgs[-1]["role"] == "user" and isinstance(msgs[-1]["content"], list) \
                        and all(b.get("type") == "tool_result" for b in msgs[-1]["content"]):
                    msgs[-1]["content"].append(block)
                else:
                    msgs.append({"role": "user", "content": [block]})
                continue
            if role == "assistant" and m.get("tool_calls"):
                content: list[dict] = []
                if m.get("content"):
                    content.append({"type": "text", "text": m["content"]})
                content += [{"type": "tool_use", "id": tc["id"], "name": wire_name(tc["name"]),
                             "input": tc.get("args") or {}} for tc in m["tool_calls"]]
                msgs.append({"role": "assistant", "content": content})
                continue
            msgs.append({"role": role, "content": m.get("content") or ""})
        p: dict[str, Any] = {"model": req.model, "messages": msgs,
                             "max_tokens": req.max_tokens or (64000 if stream else 16000)}
        if system:
            p["system"] = "\n\n".join(system)
        if req.stop:
            p["stop_sequences"] = req.stop
        if req.tools:
            p["tools"] = [{"name": wire_name(t.name), "description": t.description, "input_schema": t.input_schema,
                           **({"eager_input_streaming": True} if stream else {})} for t in req.tools]
        if req.response_schema:
            p["output_config"] = {"format": {"type": "json_schema", "schema": req.response_schema}}
        ext = dict(req.extensions)
        fallbacks = ext.pop("fallbacks", "default")
        if req.model in FALLBACK_MODELS and fallbacks != "off":
            p["betas"] = [FALLBACK_BETA]
            p["fallbacks"] = fallbacks
        if ext:
            p["extra_body"] = ext
        return p

    @staticmethod
    def _result(msg: Any, model: str) -> ChatResult:
        text, tcs = [], []
        for b in msg.content:
            if b.type == "text":
                text.append(b.text)
            elif b.type == "tool_use":
                args = b.input if isinstance(b.input, dict) else {"_invalid_json": True}
                tcs.append({"id": b.id, "name": unwire_name(b.name), "args": args})
        finish = _FINISH.get(msg.stop_reason or "end_turn", "stop")
        if finish in ("length", "refusal") and tcs:
            tcs = []  # truncated or refused tool input must not be executed
        return ChatResult("".join(text), tcs, finish, msg.usage.input_tokens, msg.usage.output_tokens,
                          getattr(msg, "model", model) or model)

    def _map_error(self, e: Exception) -> UARError:
        if isinstance(e, anthropic.APITimeoutError):
            return UARError("deadline_exceeded", f"{self.id}: timed out")
        if isinstance(e, anthropic.APIConnectionError):
            return ProviderUnavailable(self.id, type(e).__name__)
        if isinstance(e, anthropic.RateLimitError):
            return UARError("rate_limited", f"{self.id}: provider rate limit")
        if isinstance(e, anthropic.NotFoundError):
            return UARError("not_found", f"{self.id}: model or endpoint not found")
        if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            return UARError("provider_error", f"{self.id}: provider rejected credentials", retryable=False)
        if isinstance(e, anthropic.BadRequestError):
            return UARError("provider_error", f"{self.id}: provider rejected the request", retryable=False,
                            details={"message": str(e)[:300]})
        if isinstance(e, anthropic.APIStatusError):
            return UARError("provider_error", f"{self.id}: HTTP {e.status_code}", retryable=e.status_code >= 500)
        if isinstance(e, ValueError):
            return UARError("invalid_model_output", f"{self.id}: unparseable tool input")
        return UARError("provider_error", f"{self.id}: {type(e).__name__}")

    async def chat(self, req: ChatRequest) -> ChatResult:
        client = self.client()
        try:
            msg = await client.beta.messages.create(**self._params(req, False))
        except (anthropic.AnthropicError, ValueError) as e:
            raise self._map_error(e) from e
        return self._result(msg, req.model)

    async def stream(self, req: ChatRequest) -> AsyncIterator[str | ChatResult]:
        client = self.client()
        try:
            async with client.beta.messages.stream(**self._params(req, True)) as s:
                async for event in s:
                    if event.type == "text" and event.text:
                        yield event.text
                final = await s.get_final_message()
        except (anthropic.AnthropicError, ValueError) as e:
            raise self._map_error(e) from e
        yield self._result(final, req.model)

    async def list_models(self) -> list[str]:
        return list(self.cfg.models)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
