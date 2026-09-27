"""Provider adapter interface and normalised types."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from ...errors import UARError


@dataclass
class ToolSpec:
    name: str            # UAR tool name, e.g. fs.read_text
    description: str
    input_schema: dict


@dataclass
class ChatRequest:
    model: str
    messages: list[dict]                  # {role, content, tool_calls?, tool_call_id?, name?}
    tools: list[ToolSpec] = field(default_factory=list)
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop: list[str] = field(default_factory=list)
    response_schema: dict | None = None
    extensions: dict = field(default_factory=dict)  # this provider's namespaced extensions only
    # All namespaced extensions from the request ({provider_id: {...}}); the router copies the
    # entry for whichever provider it is about to call into `extensions`.
    namespaced_extensions: dict = field(default_factory=dict)


@dataclass
class ChatResult:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)   # {id, name, args}
    finish_reason: str = "stop"                            # stop | length | tool_calls | refusal
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str = ""


class ProviderUnavailable(UARError):
    """Connection-level failure before any output: eligible for circuit breaking and fallback."""

    def __init__(self, provider: str, reason: str):
        super().__init__("unavailable", f"provider {provider} unavailable", details={"reason": reason})


class Adapter:
    id: str
    capabilities: set[str]

    async def chat(self, req: ChatRequest) -> ChatResult:  # pragma: no cover - interface
        raise NotImplementedError

    def stream(self, req: ChatRequest) -> AsyncIterator[str | ChatResult]:  # pragma: no cover
        """Yield text deltas, then exactly one final ChatResult."""
        raise NotImplementedError

    async def list_models(self) -> list[str]:
        return []

    async def aclose(self) -> None:
        pass


# ------------------------------------------------------------------ helpers

def wire_name(name: str) -> str:
    """Provider function names allow [A-Za-z0-9_-]; UAR tool names use dots."""
    return name.replace(".", "__")


def unwire_name(name: str) -> str:
    return name.replace("__", ".")


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4)) if text else 0


def estimate_messages(messages: list[dict]) -> int:
    return sum(estimate_tokens(str(m.get("content") or "")) + 4 for m in messages)


def http_error(provider: str, resp: httpx.Response) -> UARError:
    body = resp.text[:300]
    if resp.status_code == 404:
        return UARError("not_found", f"{provider}: model or endpoint not found", details={"body": body})
    if resp.status_code in (401, 403):
        return UARError("provider_error", f"{provider}: provider rejected credentials", retryable=False)
    if resp.status_code == 429:
        return UARError("rate_limited", f"{provider}: provider rate limit", details={"body": body})
    if resp.status_code == 400:
        return UARError("provider_error", f"{provider}: provider rejected the request", retryable=False,
                        details={"body": body})
    return UARError("provider_error", f"{provider}: HTTP {resp.status_code}", retryable=resp.status_code >= 500,
                    details={"body": body})


def connect_error(provider: str, e: Exception) -> UARError:
    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError)):
        return ProviderUnavailable(provider, type(e).__name__)
    if isinstance(e, httpx.TimeoutException):
        return UARError("deadline_exceeded", f"{provider}: timed out", details={"reason": type(e).__name__})
    return UARError("provider_error", f"{provider}: {type(e).__name__}")


def parse_json_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {"value": v}
    except json.JSONDecodeError:
        return {"_raw": str(raw)[:2000], "_invalid_json": True}


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> Any:
    """Parse model JSON output, tolerating code fences and leading prose."""
    t = _FENCE.sub("", text.strip()).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        for open_, close in (("{", "}"), ("[", "]")):
            i, j = t.find(open_), t.rfind(close)
            if i != -1 and j > i:
                try:
                    return json.loads(t[i:j + 1])
                except json.JSONDecodeError:
                    pass
    raise UARError("invalid_model_output", "model did not return valid JSON")


async def iter_sse(resp: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    """Yield (event, data) pairs from a text/event-stream response."""
    event, data = "", []
    async for line in resp.aiter_lines():
        if line == "":
            if data:
                yield event or "message", "\n".join(data)
            event, data = "", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield event or "message", "\n".join(data)


class UnavailableAdapter(Adapter):
    """Placeholder for a provider whose backend is not running (e.g. a plugin before activation)."""

    def __init__(self, cfg, reason: str):
        self.id, self.capabilities, self.reason = cfg.id, set(cfg.capabilities), reason

    async def chat(self, req: ChatRequest) -> ChatResult:
        raise UARError("unavailable", f"{self.id}: {self.reason}", retryable=False)

    async def stream(self, req: ChatRequest):
        raise UARError("unavailable", f"{self.id}: {self.reason}", retryable=False)
        yield  # pragma: no cover
