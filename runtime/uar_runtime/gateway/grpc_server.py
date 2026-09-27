"""gRPC transport for uar.v1.Runtime. Same service layer and contract checks as HTTP."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator, Awaitable, Callable

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from uarpb.v1 import runtime_pb2 as pb
from uarpb.v1 import runtime_pb2_grpc as pbg

from ..errors import UARError
from ..governance import Principal
from ..observability import REQUEST_SECONDS, REQUESTS, get_tracer
from ..service import RuntimeService
from .codec import event_json, from_proto, to_proto
from .http import request_id_from

log = logging.getLogger("uar.grpc")


class RuntimeServicer(pbg.RuntimeServicer):
    def __init__(self, svc: RuntimeService):
        self.svc = svc

    def _principal(self, ctx: grpc.aio.ServicerContext) -> tuple[Principal, str]:
        md = {k.lower(): v for k, v in ctx.invocation_metadata() or ()}
        auth = md.get("authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        rid = request_id_from(md.get("x-request-id"))
        return self.svc.auth.authenticate(md.get("x-api-key"), bearer), rid

    async def _abort(self, ctx, e: UARError, rid: str, op: str) -> None:
        REQUESTS.labels("grpc", op, e.code).inc()
        await ctx.send_initial_metadata((("x-request-id", rid),))
        ctx.set_trailing_metadata((("uar-error", json.dumps(e.to_dict(rid))),))
        await ctx.abort(e.grpc_status, e.message)

    async def _unary(self, ctx, op: str, fn: Callable[[Principal, str], Awaitable[dict]], out_cls) -> Any:
        t0 = time.monotonic()
        rid = request_id_from(None)
        try:
            with get_tracer().start_as_current_span(f"grpc {op}"):
                p, rid = self._principal(ctx)
                async with self.svc.admit(p):
                    result = await fn(p, rid)
                msg = to_proto(out_cls, result)
        except UARError as e:
            await self._abort(ctx, e, rid, op)
            return None
        except Exception:
            log.exception("grpc %s failed", op)
            await self._abort(ctx, UARError("internal", "internal error"), rid, op)
            return None
        REQUESTS.labels("grpc", op, "ok").inc()
        REQUEST_SECONDS.labels("grpc", op).observe(time.monotonic() - t0)
        await ctx.send_initial_metadata((("x-request-id", rid),))
        return msg

    async def _stream(self, ctx, op: str, fn: Callable[[Principal, str], AsyncIterator[dict]]):
        rid = request_id_from(None)
        try:
            p, rid = self._principal(ctx)
            async with self.svc.admit(p):
                await ctx.send_initial_metadata((("x-request-id", rid),))
                async for ev in fn(p, rid):
                    checked = event_json(ev)
                    checked.pop("type")
                    yield to_proto(pb.Event, checked)
            REQUESTS.labels("grpc", op, "ok").inc()
        except UARError as e:
            await self._abort(ctx, e, rid, op)

    # ------------------------------------------------------------ RPCs

    async def Infer(self, request, context):
        req = from_proto(request)
        return await self._unary(context, "Infer", lambda p, r: self.svc.infer(p, req, r), pb.InferenceResponse)

    async def InferStream(self, request, context):
        req = from_proto(request)
        async for ev in self._stream(context, "InferStream", lambda p, r: self.svc.infer_stream(p, req, r)):
            yield ev

    async def ExecuteTool(self, request, context):
        req = from_proto(request)
        return await self._unary(context, "ExecuteTool", lambda p, r: self.svc.execute_tool(p, req, r), pb.ToolResult)

    async def ListTools(self, request, context):
        return await self._unary(context, "ListTools", lambda p, r: self.svc.list_tools(p), pb.ListToolsResponse)

    async def ListModels(self, request, context):
        return await self._unary(context, "ListModels", lambda p, r: self.svc.list_models(p), pb.ListModelsResponse)

    async def RegisterAgent(self, request, context):
        req = from_proto(request)
        return await self._unary(context, "RegisterAgent", lambda p, r: self.svc.register_agent(p, req, r),
                                 pb.AgentVersion)

    async def StartRun(self, request, context):
        req = from_proto(request)
        return await self._unary(context, "StartRun", lambda p, r: self.svc.start_run(p, req, r), pb.Run)

    async def GetRun(self, request, context):
        return await self._unary(context, "GetRun", lambda p, r: self.svc.get_run(p, request.run_id), pb.Run)

    async def WatchRun(self, request, context):
        async for ev in self._stream(context, "WatchRun",
                                     lambda p, r: self.svc.watch_run(p, request.run_id, request.after_seq)):
            yield ev

    async def CancelRun(self, request, context):
        return await self._unary(context, "CancelRun",
                                 lambda p, r: self.svc.cancel_run(p, request.run_id, request.reason, r), pb.Run)

    async def ResolveRun(self, request, context):
        return await self._unary(context, "ResolveRun",
                                 lambda p, r: self.svc.resolve_run(p, request.run_id, request.action, request.note, r),
                                 pb.Run)

    async def DryRun(self, request, context):
        req = from_proto(request)
        return await self._unary(context, "DryRun", lambda p, r: self.svc.dry_run(p, req, r), pb.DryRunReport)

    async def DecideApproval(self, request, context):
        req = from_proto(request)
        return await self._unary(context, "DecideApproval", lambda p, r: self.svc.decide_approval(p, req, r),
                                 pb.Approval)


async def start_grpc(svc: RuntimeService, host: str, port: int) -> grpc.aio.Server:
    server = grpc.aio.server(options=[("grpc.max_receive_message_length", svc.s.server.max_body_bytes)])
    pbg.add_RuntimeServicer_to_server(RuntimeServicer(svc), server)
    hs = health.aio.HealthServicer() if hasattr(health, "aio") else health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(hs, server)
    bound = server.add_insecure_port(f"{host}:{port}")
    await server.start()
    try:
        await hs.set("uar.v1.Runtime", health_pb2.HealthCheckResponse.SERVING)
    except TypeError:
        hs.set("uar.v1.Runtime", health_pb2.HealthCheckResponse.SERVING)
    server.bound_port = bound  # type: ignore[attr-defined]
    return server
