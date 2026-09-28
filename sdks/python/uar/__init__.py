"""Python client for the Universal AI Runtime (HTTP/JSON + SSE).

    from uar import Client

    client = Client("http://localhost:9000")          # API key from UAR_API_KEY
    resp = client.inference(model="local:default", prompt="Explain quantum computing")
    print(resp.text)

    # A registered application (token service): access tokens are fetched and renewed automatically.
    client = Client("http://localhost:9000", client_id="billing-service-1a2b3c4d",
                    client_secret=os.environ["UAR_CLIENT_SECRET"])

Retries: only GET requests and POSTs that carry an idempotency key are retried (on 429, 503 and
connection errors). Inference and tool calls without a key are never retried automatically.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import httpx

__all__ = ["Client", "AsyncClient", "UARError", "AuthenticationError", "PermissionDeniedError", "NotFoundError",
           "ConflictError", "InvalidRequestError", "RateLimitError", "UnavailableError", "ServerError", "Resource",
           "Event"]
__version__ = "0.6.0"


# ------------------------------------------------------------------ results & errors

class Resource(dict):
    """A response object: a dict with attribute access (resp.content == resp["content"])."""

    def __getattr__(self, name: str) -> Any:
        try:
            v = self[name]
        except KeyError:
            raise AttributeError(name) from None
        return Resource(v) if isinstance(v, dict) and not isinstance(v, Resource) else v


class InferenceResponse(Resource):
    @property
    def text(self) -> str:
        return self.get("content", "")


class Event(Resource):
    """One stream event. `event.type` names the populated body ("token", "completed", ...)."""

    @property
    def body(self) -> Any:
        return self.get(self.get("type", ""))


class UARError(Exception):
    def __init__(self, status: int, error: dict[str, Any]):
        self.status = status
        self.code = error.get("code", "unknown")
        self.message = error.get("message", "")
        self.request_id = error.get("request_id", "")
        self.retryable = bool(error.get("retryable", False))
        self.details = error.get("details") or {}
        super().__init__(f"{self.code}: {self.message} (status {status}, request {self.request_id})")


class InvalidRequestError(UARError): ...
class AuthenticationError(UARError): ...
class PermissionDeniedError(UARError): ...
class NotFoundError(UARError): ...
class ConflictError(UARError): ...
class RateLimitError(UARError): ...
class ServerError(UARError): ...
class UnavailableError(ServerError): ...


def _error(status: int, payload: Any) -> UARError:
    err = payload.get("error") if isinstance(payload, dict) else None
    err = err if isinstance(err, dict) else {"code": "http_error", "message": str(payload)[:300]}
    cls = {400: InvalidRequestError, 401: AuthenticationError, 403: PermissionDeniedError, 404: NotFoundError,
           409: ConflictError, 413: InvalidRequestError, 422: InvalidRequestError, 429: RateLimitError,
           503: UnavailableError}.get(status, ServerError if status >= 500 else UARError)
    return cls(status, err)


# ------------------------------------------------------------------ request building (shared)

def _inference_body(model: str | None, prompt: str | None, *, messages=None, agent=None, tools=None,
                    tool_mode=None, stream=False, temperature=None, max_tokens=None, top_p=None, stop=None,
                    response_schema=None, data_class=None, extensions=None) -> dict:
    body: dict[str, Any] = {}
    if model:
        body["model"] = model
    if messages:
        body["messages"] = messages
    elif prompt is not None:
        body["input"] = prompt
    for k, v in (("agent", agent), ("tools", tools), ("tool_mode", tool_mode), ("data_class", data_class),
                 ("extensions", extensions)):
        if v:
            body[k] = v
    params = {k: v for k, v in (("temperature", temperature), ("max_tokens", max_tokens), ("top_p", top_p),
                                ("stop", stop), ("response_schema", response_schema)) if v is not None}
    if params:
        body["params"] = params
    if stream:
        body["stream"] = True
    return body


def _definition(definition: dict | str | os.PathLike) -> dict:
    if isinstance(definition, dict):
        return definition
    import yaml  # optional dependency, only for YAML files
    return yaml.safe_load(Path(definition).read_text(encoding="utf-8"))


def _sse_lines_to_events(lines: Iterator[str]) -> Iterator[Event]:
    data: list[str] = []
    for line in lines:
        if line == "":
            if data:
                yield Event(json.loads("\n".join(data)))
            data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield Event(json.loads("\n".join(data)))


TERMINAL = ("completed", "error")


TOKEN_PATH = "/api/v1/oauth/token"


class _Base:
    def __init__(self, base_url: str = "http://localhost:9000", api_key: str | None = None, *,
                 token: str | None = None, client_id: str | None = None, client_secret: str | None = None,
                 scope: str | None = None, timeout: float = 120.0, max_retries: int = 2):
        """Credentials, first match wins: client_id + client_secret (a registered application; access
        tokens are fetched from the runtime's token service and renewed before they expire), api_key
        (or UAR_API_KEY), token (a bearer token you obtained yourself). UAR_CLIENT_ID and
        UAR_CLIENT_SECRET are used when no credential is passed."""
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id or (os.environ.get("UAR_CLIENT_ID") if api_key is None and token is None else None)
        self.client_secret = client_secret or (os.environ.get("UAR_CLIENT_SECRET") if self.client_id else None)
        if self.client_id and not self.client_secret:
            raise ValueError("client_secret is required with client_id")
        self.scope = scope
        self.api_key = None if self.client_id else (api_key if api_key is not None else os.environ.get("UAR_API_KEY"))
        self.token = token
        self._token_expires = float("inf") if token else 0.0
        self.timeout = timeout
        self.max_retries = max_retries

    def _token_form(self) -> dict[str, str]:
        f = {"grant_type": "client_credentials", "client_id": self.client_id or "", "client_secret": self.client_secret or ""}
        if self.scope:
            f["scope"] = self.scope
        return f

    def _accept_token(self, resp: httpx.Response) -> None:
        body = _json(resp)
        if resp.status_code >= 400:
            raise _error(resp.status_code, {"error": {"code": body.get("error", "unauthenticated"),
                                                      "message": body.get("error_description", "token request failed")}})
        self.token = body["access_token"]
        self._token_expires = time.time() + float(body.get("expires_in", 300))

    def _token_stale(self) -> bool:
        return bool(self.client_id) and (not self.token or time.time() > self._token_expires - 60)

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        h = {"User-Agent": f"uar-python/{__version__}", "Accept": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        elif self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        return h

    @staticmethod
    def _retryable(method: str, headers: dict) -> bool:
        return method == "GET" or "Idempotency-Key" in headers

    def _delay(self, attempt: int, resp: httpx.Response | None) -> float:
        ra = resp.headers.get("retry-after") if resp is not None else None
        if ra and ra.isdigit():
            return min(float(ra), 30.0)
        return min(0.25 * 2 ** attempt, 5.0) * (0.5 + random.random())


# ------------------------------------------------------------------ synchronous client

class Client(_Base):
    def __init__(self, base_url: str = "http://localhost:9000", api_key: str | None = None, **kw: Any):
        super().__init__(base_url, api_key, **kw)
        self._http = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _ensure_token(self) -> None:
        if self._token_stale():
            self._accept_token(self._http.post(TOKEN_PATH, data=self._token_form()))

    def _request(self, method: str, path: str, body: dict | None = None, idempotency_key: str | None = None,
                 params: dict | None = None) -> Any:
        self._ensure_token()
        headers = self._headers(idempotency_key)
        attempt, renewed = 0, False
        while True:
            resp = None
            try:
                resp = self._http.request(method, path, json=body, headers=headers, params=params)
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code == 401 and self.client_id and not renewed:   # token revoked or rotated
                    self.token, renewed = None, True
                    self._ensure_token()
                    headers = self._headers(idempotency_key)
                    continue
                if resp.status_code not in (429, 503) or not self._retryable(method, headers) \
                        or attempt >= self.max_retries:
                    raise _error(resp.status_code, _json(resp))
            except httpx.TransportError:
                if not self._retryable(method, headers) or attempt >= self.max_retries:
                    raise
            time.sleep(self._delay(attempt, resp))
            attempt += 1

    def _stream(self, method: str, path: str, body: dict | None = None, params: dict | None = None
                ) -> Iterator[Event]:
        self._ensure_token()
        headers = {**self._headers(), "Accept": "text/event-stream"}
        with self._http.stream(method, path, json=body, headers=headers, params=params,
                               timeout=httpx.Timeout(self.timeout, read=None)) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise _error(resp.status_code, _json(resp))
            yield from _sse_lines_to_events(resp.iter_lines())

    # inference ------------------------------------------------------
    def inference(self, model: str | None = None, prompt: str | None = None, agent: str | None = None,
                  **kw: Any) -> InferenceResponse:
        """Synchronous inference. With `agent`, runs that agent on the prompt and returns its result."""
        return InferenceResponse(self._request("POST", "/api/v1/inference",
                                               _inference_body(model, prompt, agent=agent, **kw)))

    def stream(self, model: str | None = None, prompt: str | None = None, **kw: Any) -> Iterator[Event]:
        """Streaming inference: yields Events (started, token..., usage, completed | error)."""
        return self._stream("POST", "/api/v1/inference", _inference_body(model, prompt, stream=True, **kw))

    # tools & catalog -------------------------------------------------
    def execute_tool(self, tool: str, args: dict | None = None, idempotency_key: str | None = None) -> Resource:
        return Resource(self._request("POST", "/api/v1/tool/execute", {"tool": tool, "args": args or {}},
                                      idempotency_key))

    def list_models(self) -> list[Resource]:
        return [Resource(m) for m in self._request("GET", "/api/v1/models").get("models", [])]

    def list_tools(self) -> list[Resource]:
        return [Resource(t) for t in self._request("GET", "/api/v1/tools").get("tools", [])]

    # agents & runs ---------------------------------------------------
    def register_agent(self, definition: dict | str | os.PathLike) -> Resource:
        return Resource(self._request("POST", "/api/v1/agents", {"definition": _definition(definition)}))

    def run_agent(self, agent_id: str, input: dict | None = None, *, version: str | None = None,
                  idempotency_key: str | None = None, wait: bool = False, timeout: float = 600) -> Resource:
        body: dict[str, Any] = {"agent_id": agent_id, "input": input or {}}
        if version:
            body["version"] = version
        run = Resource(self._request("POST", "/api/v1/agent/run", body, idempotency_key))
        return self.wait_run(run["run_id"], timeout) if wait else run

    def get_run(self, run_id: str) -> Resource:
        return Resource(self._request("GET", f"/api/v1/runs/{run_id}"))

    def wait_run(self, run_id: str, timeout: float = 600) -> Resource:
        deadline = time.monotonic() + timeout
        while True:
            run = self.get_run(run_id)
            if run["status"] in ("succeeded", "failed", "cancelled", "needs_attention"):
                return run
            if time.monotonic() > deadline:
                raise TimeoutError(f"run {run_id} still {run['status']}")
            time.sleep(0.25)

    def watch_run(self, run_id: str, after_seq: int = 0) -> Iterator[Event]:
        return self._stream("GET", f"/api/v1/runs/{run_id}/events", params={"after_seq": after_seq})

    def cancel_run(self, run_id: str, reason: str = "") -> Resource:
        return Resource(self._request("POST", f"/api/v1/runs/{run_id}/cancel", {"reason": reason} if reason else {}))

    def resolve_run(self, run_id: str, action: str, note: str = "") -> Resource:
        return Resource(self._request("POST", f"/api/v1/runs/{run_id}/resolve", {"action": action, "note": note}))

    def dry_run(self, agent_id: str | None = None, *, definition: dict | str | None = None,
                inference: dict | None = None, mode: str = "static", input: dict | None = None,
                fixtures: dict | None = None, version: str | None = None) -> Resource:
        return Resource(self._request("POST", "/api/v1/dry-run", _dry_body(agent_id, definition, inference, mode,
                                                                           input, fixtures, version)))


def _dry_body(agent_id, definition, inference, mode, input_, fixtures, version) -> dict:
    body: dict[str, Any] = {"mode": mode}
    if agent_id:
        body["agent_id"] = agent_id
    elif definition is not None:
        body["definition"] = _definition(definition)
    elif inference is not None:
        body["inference"] = inference
    for k, v in (("input", input_), ("fixtures", fixtures), ("version", version)):
        if v:
            body[k] = v
    return body


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"error": {"code": "http_error", "message": resp.text[:300]}}


# ------------------------------------------------------------------ asynchronous client

class AsyncClient(_Base):
    def __init__(self, base_url: str = "http://localhost:9000", api_key: str | None = None, **kw: Any):
        super().__init__(base_url, api_key, **kw)
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _ensure_token(self) -> None:
        if self._token_stale():
            self._accept_token(await self._http.post(TOKEN_PATH, data=self._token_form()))

    async def _request(self, method: str, path: str, body: dict | None = None, idempotency_key: str | None = None,
                       params: dict | None = None) -> Any:
        await self._ensure_token()
        headers = self._headers(idempotency_key)
        attempt, renewed = 0, False
        while True:
            resp = None
            try:
                resp = await self._http.request(method, path, json=body, headers=headers, params=params)
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code == 401 and self.client_id and not renewed:
                    self.token, renewed = None, True
                    await self._ensure_token()
                    headers = self._headers(idempotency_key)
                    continue
                if resp.status_code not in (429, 503) or not self._retryable(method, headers) \
                        or attempt >= self.max_retries:
                    raise _error(resp.status_code, _json(resp))
            except httpx.TransportError:
                if not self._retryable(method, headers) or attempt >= self.max_retries:
                    raise
            await asyncio.sleep(self._delay(attempt, resp))
            attempt += 1

    async def _stream(self, method: str, path: str, body: dict | None = None, params: dict | None = None
                      ) -> AsyncIterator[Event]:
        await self._ensure_token()
        headers = {**self._headers(), "Accept": "text/event-stream"}
        async with self._http.stream(method, path, json=body, headers=headers, params=params,
                                     timeout=httpx.Timeout(self.timeout, read=None)) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                raise _error(resp.status_code, _json(resp))
            data: list[str] = []
            async for line in resp.aiter_lines():
                if line == "":
                    if data:
                        yield Event(json.loads("\n".join(data)))
                    data = []
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
            if data:
                yield Event(json.loads("\n".join(data)))

    async def inference(self, model: str | None = None, prompt: str | None = None, agent: str | None = None,
                        **kw: Any) -> InferenceResponse:
        return InferenceResponse(await self._request("POST", "/api/v1/inference",
                                                     _inference_body(model, prompt, agent=agent, **kw)))

    def stream(self, model: str | None = None, prompt: str | None = None, **kw: Any) -> AsyncIterator[Event]:
        return self._stream("POST", "/api/v1/inference", _inference_body(model, prompt, stream=True, **kw))

    async def execute_tool(self, tool: str, args: dict | None = None, idempotency_key: str | None = None) -> Resource:
        return Resource(await self._request("POST", "/api/v1/tool/execute", {"tool": tool, "args": args or {}},
                                            idempotency_key))

    async def list_models(self) -> list[Resource]:
        return [Resource(m) for m in (await self._request("GET", "/api/v1/models")).get("models", [])]

    async def list_tools(self) -> list[Resource]:
        return [Resource(t) for t in (await self._request("GET", "/api/v1/tools")).get("tools", [])]

    async def register_agent(self, definition: dict | str | os.PathLike) -> Resource:
        return Resource(await self._request("POST", "/api/v1/agents", {"definition": _definition(definition)}))

    async def run_agent(self, agent_id: str, input: dict | None = None, *, version: str | None = None,
                        idempotency_key: str | None = None, wait: bool = False, timeout: float = 600) -> Resource:
        body: dict[str, Any] = {"agent_id": agent_id, "input": input or {}}
        if version:
            body["version"] = version
        run = Resource(await self._request("POST", "/api/v1/agent/run", body, idempotency_key))
        return await self.wait_run(run["run_id"], timeout) if wait else run

    async def get_run(self, run_id: str) -> Resource:
        return Resource(await self._request("GET", f"/api/v1/runs/{run_id}"))

    async def wait_run(self, run_id: str, timeout: float = 600) -> Resource:
        deadline = time.monotonic() + timeout
        while True:
            run = await self.get_run(run_id)
            if run["status"] in ("succeeded", "failed", "cancelled", "needs_attention"):
                return run
            if time.monotonic() > deadline:
                raise TimeoutError(f"run {run_id} still {run['status']}")
            await asyncio.sleep(0.25)

    def watch_run(self, run_id: str, after_seq: int = 0) -> AsyncIterator[Event]:
        return self._stream("GET", f"/api/v1/runs/{run_id}/events", params={"after_seq": after_seq})

    async def cancel_run(self, run_id: str, reason: str = "") -> Resource:
        return Resource(await self._request("POST", f"/api/v1/runs/{run_id}/cancel",
                                            {"reason": reason} if reason else {}))

    async def resolve_run(self, run_id: str, action: str, note: str = "") -> Resource:
        return Resource(await self._request("POST", f"/api/v1/runs/{run_id}/resolve",
                                            {"action": action, "note": note}))

    async def dry_run(self, agent_id: str | None = None, *, definition: dict | str | None = None,
                      inference: dict | None = None, mode: str = "static", input: dict | None = None,
                      fixtures: dict | None = None, version: str | None = None) -> Resource:
        return Resource(await self._request("POST", "/api/v1/dry-run", _dry_body(agent_id, definition, inference,
                                                                                 mode, input, fixtures, version)))
