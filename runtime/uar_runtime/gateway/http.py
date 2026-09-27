"""HTTP/JSON + SSE + WebSocket transport (FastAPI)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import yaml
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from uarpb.v1 import runtime_pb2 as pb

from ..errors import UARError
from ..governance import Principal
from ..observability import REGISTRY, REQUEST_SECONDS, REQUESTS, get_tracer
from ..service import RuntimeService
from .codec import check_response, event_json, validate_request

log = logging.getLogger("uar.http")
OPENAPI = Path(__file__).resolve().parents[3] / "contracts" / "generated" / "openapi.json"
_RID = re.compile(r"[A-Za-z0-9._-]{8,64}")


def request_id_from(value: str | None) -> str:
    return value if value and _RID.fullmatch(value) else "req_" + secrets.token_hex(8)


def error_response(e: UARError, rid: str) -> JSONResponse:
    headers = {"X-Request-ID": rid}
    if e.code == "rate_limited":
        headers["Retry-After"] = str(max(1, int(float(e.details.get("retry_after_s", 1)))))
    return JSONResponse({"error": e.to_dict(rid)}, status_code=e.http_status, headers=headers)


def sse(event: dict) -> bytes:
    return f"id: {event.get('seq', '')}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


def create_app(svc: RuntimeService) -> FastAPI:
    s = svc.s
    app = FastAPI(title="Universal AI Runtime", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def envelope(request: Request, call_next):
        rid = request_id_from(request.headers.get("x-request-id"))
        request.state.request_id = rid
        t0 = time.monotonic()
        route = request.url.path
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > s.server.max_body_bytes:
            resp = error_response(UARError("payload_too_large", f"body exceeds {s.server.max_body_bytes} bytes"), rid)
        else:
            with get_tracer().start_as_current_span(f"{request.method} {route}") as span:
                span.set_attributes({"http.method": request.method, "http.route": route, "uar.request_id": rid})
                try:
                    resp = await call_next(request)
                except UARError as e:
                    resp = error_response(e, rid)
                except Exception:
                    log.exception("unhandled error")
                    resp = error_response(UARError("internal", "internal error"), rid)
                span.set_attribute("http.status_code", resp.status_code)
        resp.headers["X-Request-ID"] = rid
        op = request.scope.get("route").path if request.scope.get("route") else "unmatched"
        REQUESTS.labels("http", op, str(resp.status_code)).inc()
        REQUEST_SECONDS.labels("http", op).observe(time.monotonic() - t0)
        return resp

    @app.exception_handler(UARError)
    async def uar_error(request: Request, e: UARError):
        return error_response(e, getattr(request.state, "request_id", request_id_from(None)))

    def principal(request: Request) -> Principal:
        auth = request.headers.get("authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        return svc.auth.authenticate(request.headers.get("x-api-key"), bearer)

    async def body(request: Request, cls) -> dict:
        raw = await request.body()
        if len(raw) > s.server.max_body_bytes:
            raise UARError("payload_too_large", f"body exceeds {s.server.max_body_bytes} bytes")
        ctype = request.headers.get("content-type", "")
        try:
            if "yaml" in ctype:
                data = {"definition": yaml.safe_load(raw)}
            else:
                data = json.loads(raw or b"{}")
        except (json.JSONDecodeError, yaml.YAMLError):
            raise UARError("invalid_argument", "body is not valid JSON" + (" or YAML" if "yaml" in ctype else ""))
        return validate_request(cls, data)

    async def call(request: Request, fn: Callable[[Principal, str], Awaitable[dict]], out_cls) -> JSONResponse:
        p = principal(request)
        rid = request.state.request_id
        async with svc.admit(p):
            result = await fn(p, rid)
        return JSONResponse(check_response(out_cls, result))

    def stream_response(p: Principal, gen_factory: Callable[[], AsyncIterator[dict]]) -> StreamingResponse:
        async def gen():
            async with svc.admit(p):
                async for ev in gen_factory():
                    yield sse(event_json(ev))
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---------------------------------------------------------------- health & contract

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        r = await svc.ready()
        return JSONResponse(r, status_code=200 if r["ready"] else 503)

    @app.get("/metrics")
    async def metrics(request: Request):
        if not s.server.public_metrics:
            principal(request).require("admin")
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/v1/openapi.json")
    async def openapi():
        return Response(OPENAPI.read_bytes(), media_type="application/json")

    # ---------------------------------------------------------------- inference & tools

    @app.post("/api/v1/inference")
    async def inference(request: Request):
        req = await body(request, pb.InferenceRequest)
        rid = request.state.request_id
        if req.get("stream"):
            p = principal(request)
            return stream_response(p, lambda: svc.infer_stream(p, req, rid))
        return await call(request, lambda p, r: svc.infer(p, req, r), pb.InferenceResponse)

    @app.post("/api/v1/tool/execute")
    async def tool_execute(request: Request):
        req = await body(request, pb.ToolRequest)
        if request.headers.get("idempotency-key"):
            req.setdefault("idempotency_key", request.headers["idempotency-key"])
        return await call(request, lambda p, r: svc.execute_tool(p, req, r), pb.ToolResult)

    @app.get("/api/v1/tools")
    async def tools(request: Request):
        return await call(request, lambda p, r: svc.list_tools(p), pb.ListToolsResponse)

    @app.get("/api/v1/models")
    async def models(request: Request):
        return await call(request, lambda p, r: svc.list_models(p), pb.ListModelsResponse)

    # ---------------------------------------------------------------- agents & runs

    @app.post("/api/v1/agents")
    async def register(request: Request):
        req = await body(request, pb.RegisterAgentRequest)
        return await call(request, lambda p, r: svc.register_agent(p, req, r), pb.AgentVersion)

    @app.post("/api/v1/agent/run", status_code=202)
    async def start_run(request: Request):
        req = await body(request, pb.RunRequest)
        if request.headers.get("idempotency-key"):
            req.setdefault("idempotency_key", request.headers["idempotency-key"])
        resp = await call(request, lambda p, r: svc.start_run(p, req, r), pb.Run)
        resp.status_code = 202
        return resp

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(request: Request, run_id: str):
        return await call(request, lambda p, r: svc.get_run(p, run_id), pb.Run)

    @app.get("/api/v1/runs/{run_id}/events")
    async def run_events(request: Request, run_id: str, after_seq: int = 0):
        p = principal(request)
        last = request.headers.get("last-event-id", "")
        after = int(last) if last.isdigit() else after_seq
        await svc.get_run(p, run_id)  # 404 before opening the stream
        return stream_response(p, lambda: svc.watch_run(p, run_id, after))

    @app.post("/api/v1/runs/{run_id}/cancel")
    async def cancel(request: Request, run_id: str):
        req = await body(request, pb.CancelRunRequest)
        return await call(request, lambda p, r: svc.cancel_run(p, run_id, req.get("reason", ""), r), pb.Run)

    @app.post("/api/v1/runs/{run_id}/resolve")
    async def resolve(request: Request, run_id: str):
        req = await body(request, pb.ResolveRunRequest)
        return await call(request, lambda p, r: svc.resolve_run(p, run_id, req.get("action", ""), req.get("note", ""),
                                                                 r), pb.Run)

    @app.post("/api/v1/dry-run")
    async def dry_run(request: Request):
        req = await body(request, pb.DryRunRequest)
        return await call(request, lambda p, r: svc.dry_run(p, req, r), pb.DryRunReport)

    @app.post("/api/v1/plugins")
    async def register_plugin(request: Request):
        req = await body(request, pb.RegisterPluginRequest)
        return await call(request, lambda p, r: svc.register_plugin(p, req, r), pb.PluginVersion)

    @app.get("/api/v1/plugins")
    async def list_plugins(request: Request):
        return await call(request, lambda p, r: svc.list_plugins(p), pb.ListPluginsResponse)

    @app.post("/api/v1/plugins/{plugin_id}/activate")
    async def activate_plugin(request: Request, plugin_id: str):
        req = await body(request, pb.ActivatePluginRequest)
        return await call(request, lambda p, r: svc.activate_plugin(p, plugin_id, req.get("version", ""), r),
                          pb.PluginVersion)

    @app.post("/api/v1/plugins/{plugin_id}/rollback")
    async def rollback_plugin(request: Request, plugin_id: str):
        await body(request, pb.RollbackPluginRequest)
        return await call(request, lambda p, r: svc.rollback_plugin(p, plugin_id, r), pb.PluginVersion)

    @app.post("/api/v1/approvals/{approval_id}/decision")
    async def approval(request: Request, approval_id: str):
        req = await body(request, pb.ApprovalDecision)
        if req.get("approval_id") not in (None, "", approval_id):
            raise UARError("invalid_argument", "approval_id in the body does not match the path")
        req["approval_id"] = approval_id
        return await call(request, lambda p, r: svc.decide_approval(p, req, r), pb.Approval)

    @app.get("/api/v1/approvals")
    async def list_approvals(request: Request):
        q = {k: request.query_params.get(k, "") for k in ("status", "run_id")}
        return await call(request, lambda p, r: svc.list_approvals(p, q), pb.ListApprovalsResponse)

    @app.get("/api/v1/approvals/{approval_id}")
    async def get_approval(request: Request, approval_id: str):
        return await call(request, lambda p, r: svc.get_approval(p, approval_id), pb.Approval)

    @app.get("/api/v1/audit/verify")
    async def verify_audit(request: Request):
        return await call(request, lambda p, r: svc.verify_audit(p, r), pb.AuditVerification)

    @app.get("/api/v1/audit/export")
    async def export_audit(request: Request):
        try:
            q = {"after_seq": int(request.query_params.get("after_seq", "0")),
                 "limit": int(request.query_params.get("limit", "500"))}
        except ValueError:
            raise UARError("invalid_argument", "after_seq and limit must be integers") from None
        return await call(request, lambda p, r: svc.export_audit(p, q, r), pb.ExportAuditResponse)

    # ---------------------------------------------------------------- administration

    def qint(request: Request, name: str, default: int = 0) -> int:
        try:
            return int(request.query_params.get(name, default))
        except ValueError:
            raise UARError("invalid_argument", f"{name} must be an integer") from None

    @app.get("/api/v1/admin/info")
    async def runtime_info(request: Request):
        return await call(request, lambda p, r: svc.runtime_info(p), pb.RuntimeInfo)

    @app.get("/api/v1/usage")
    async def list_usage(request: Request):
        q = {"before_id": qint(request, "before_id"), "limit": qint(request, "limit", 100),
             **{k: request.query_params.get(k, "") for k in ("subject", "model", "since")}}
        return await call(request, lambda p, r: svc.list_usage(p, q), pb.ListUsageResponse)

    @app.get("/api/v1/admin/keys")
    async def list_keys(request: Request):
        inc = request.query_params.get("include_revoked", "false").lower() in ("1", "true", "yes")
        return await call(request, lambda p, r: svc.list_api_keys(p, inc), pb.ListApiKeysResponse)

    @app.post("/api/v1/admin/keys")
    async def create_key(request: Request):
        req = await body(request, pb.CreateApiKeyRequest)
        return await call(request, lambda p, r: svc.create_api_key(p, req, r), pb.CreatedApiKey)

    @app.post("/api/v1/admin/keys/{key_id}/revoke")
    async def revoke_key(request: Request, key_id: str):
        req = await body(request, pb.RevokeApiKeyRequest)
        return await call(request, lambda p, r: svc.revoke_api_key(p, key_id, req.get("reason", ""), r),
                          pb.ApiKeyInfo)

    @app.get("/api/v1/admin/access")
    async def access_policy(request: Request):
        return await call(request, lambda p, r: svc.access_policy(p), pb.AccessPolicy)

    @app.get("/api/v1/admin/logs")
    async def list_logs(request: Request):
        q = {"after_seq": qint(request, "after_seq"), "limit": qint(request, "limit", 500),
             "min_level": request.query_params.get("min_level", "")}
        return await call(request, lambda p, r: svc.list_logs(p, q), pb.ListLogsResponse)

    # ---------------------------------------------------------------- WebSocket

    @app.websocket("/api/v1/ws")
    async def ws(socket: WebSocket):
        await WebSocketSession(svc, socket).run()

    return app


class WebSocketSession:
    """Bidirectional JSON protocol. Client frames: {"id", "op", "body"}; ops: auth, infer, start_run,
    watch_run, get_run, cancel_run, cancel (stop an in-flight op), list_models, list_tools, execute_tool,
    dry_run. Server frames: {"id","event"} | {"id","result"} | {"id","error"} | {"id","done":true}.
    Authenticate with an X-API-Key/Authorization header on connect, or an initial {"op":"auth"} frame."""

    def __init__(self, svc: RuntimeService, socket: WebSocket):
        self.svc, self.ws = svc, socket
        self.p: Principal | None = None
        self.tasks: dict[str, asyncio.Task] = {}
        self.send_lock = asyncio.Lock()

    async def send(self, frame: dict) -> None:
        async with self.send_lock:
            await self.ws.send_text(json.dumps(frame))

    async def run(self) -> None:
        await self.ws.accept()
        h = self.ws.headers
        try:
            auth = h.get("authorization", "")
            if h.get("x-api-key") or auth.lower().startswith("bearer "):
                self.p = self.svc.auth.authenticate(h.get("x-api-key"), auth[7:].strip() or None)
        except UARError as e:
            await self.send({"id": "", "error": e.to_dict()})
            await self.ws.close(code=4401)
            return
        try:
            while True:
                frame = json.loads(await self.ws.receive_text())
                await self.handle(frame if isinstance(frame, dict) else {})
        except WebSocketDisconnect:
            pass
        except json.JSONDecodeError:
            await self.ws.close(code=4400)
        finally:
            for t in self.tasks.values():
                t.cancel()

    async def handle(self, f: dict) -> None:
        fid, op, b = str(f.get("id", "")), f.get("op", ""), f.get("body") or {}
        rid = request_id_from(None)
        if op == "auth":
            try:
                self.p = self.svc.auth.authenticate(b.get("api_key"), b.get("token"))
                await self.send({"id": fid, "result": {"authenticated": True, "tenant": self.p.tenant}})
            except UARError as e:
                await self.send({"id": fid, "error": e.to_dict(rid)})
            return
        if op == "cancel":
            t = self.tasks.pop(str(b.get("target", "")), None)
            if t:
                t.cancel()
            await self.send({"id": fid, "result": {"cancelled": t is not None}})
            return
        if self.p is None:
            await self.send({"id": fid, "error": UARError("unauthenticated", "authenticate first").to_dict(rid)})
            return
        self.tasks[fid] = asyncio.create_task(self.dispatch(fid, op, b, rid))
        self.tasks[fid].add_done_callback(lambda _t: self.tasks.pop(fid, None))

    async def dispatch(self, fid: str, op: str, b: dict, rid: str) -> None:
        p, svc = self.p, self.svc
        assert p is not None
        try:
            async with svc.admit(p):
                if op == "infer":
                    validate_request(pb.InferenceRequest, b)
                    async for ev in svc.infer_stream(p, b, rid):
                        await self.send({"id": fid, "event": event_json(ev)})
                elif op == "watch_run":
                    async for ev in svc.watch_run(p, b.get("run_id", ""), int(b.get("after_seq", 0))):
                        await self.send({"id": fid, "event": event_json(ev)})
                else:
                    table: dict[str, tuple[Any, Any, Callable[[], Awaitable[dict]]]] = {
                        "start_run": (pb.RunRequest, pb.Run, lambda: svc.start_run(p, b, rid)),
                        "get_run": (pb.GetRunRequest, pb.Run, lambda: svc.get_run(p, b.get("run_id", ""))),
                        "cancel_run": (pb.CancelRunRequest, pb.Run,
                                       lambda: svc.cancel_run(p, b.get("run_id", ""), b.get("reason", ""), rid)),
                        "list_models": (pb.ListModelsRequest, pb.ListModelsResponse, lambda: svc.list_models(p)),
                        "list_tools": (pb.ListToolsRequest, pb.ListToolsResponse, lambda: svc.list_tools(p)),
                        "execute_tool": (pb.ToolRequest, pb.ToolResult, lambda: svc.execute_tool(p, b, rid)),
                        "dry_run": (pb.DryRunRequest, pb.DryRunReport, lambda: svc.dry_run(p, b, rid)),
                    }
                    if op not in table:
                        raise UARError("invalid_argument", f"unknown op {op!r}")
                    in_cls, out_cls, fn = table[op]
                    validate_request(in_cls, b)
                    await self.send({"id": fid, "result": check_response(out_cls, await fn())})
                    return
            await self.send({"id": fid, "done": True})
        except asyncio.CancelledError:
            await self.send({"id": fid, "error": UARError("cancelled", "cancelled by client").to_dict(rid)})
        except UARError as e:
            await self.send({"id": fid, "error": e.to_dict(rid)})
        except Exception:
            log.exception("ws op failed")
            await self.send({"id": fid, "error": UARError("internal", "internal error").to_dict(rid)})
