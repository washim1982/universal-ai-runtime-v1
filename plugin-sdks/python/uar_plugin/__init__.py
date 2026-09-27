"""Write UAR plugins in Python.

    from uar_plugin import Plugin, serve

    class Echo(Plugin):
        id, version, kind = "acme.echo", "1.0.0", "model"
        models = ["echo-1"]

        def model(self, call, ctx):
            return {"text": "echo: " + call["messages"][-1]["content"]}

    serve(Echo())

The runtime starts the plugin with UAR_PLUGIN_ADDR set to the address to listen on and talks
uar.plugin.v1 over gRPC. Return plain dicts; the helper converts them.

Handlers return:
- model(call, ctx):  {"text", "finish_reason"?, "tool_calls"?, "input_tokens"?, "output_tokens"?}
                     or an iterator of str tokens followed by a final such dict (streaming)
- tool(name, args, ctx):   {"output": {...}} or {"text": "..."}; raise PluginError for a tool error
- agent(agent, input, ctx): {"output": {...}}
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
from concurrent import futures
from typing import Any, Iterator

import grpc
from google.protobuf import json_format, struct_pb2

from uarpb.plugin.v1 import plugin_pb2 as pb
from uarpb.plugin.v1 import plugin_pb2_grpc as pbg

__all__ = ["Plugin", "PluginError", "serve", "tool"]


class PluginError(Exception):
    def __init__(self, message: str, code: str = "plugin_error"):
        super().__init__(message)
        self.code = code


def _struct(d: dict | None) -> struct_pb2.Struct:
    s = struct_pb2.Struct()
    if d:
        json_format.ParseDict(json.loads(json.dumps(d, default=str)), s)
    return s


def _dict(s: struct_pb2.Struct) -> dict:
    return json_format.MessageToDict(s)


def tool(name: str, description: str, input_schema: dict, side_effect: str = "read"):
    """Declare a tool on a Plugin subclass method: @tool("count", "Count words", {...})."""
    def deco(fn):
        fn._uar_tool = (name, description, input_schema, side_effect)
        return fn
    return deco


class Plugin:
    id = ""
    version = ""
    kind = "model"            # model | tool | agent
    models: list[str] = []
    capabilities: list[str] = []

    def init(self, config: dict, secrets: dict) -> None:
        """Called once after start with the manifest's config and the declared secrets."""

    def model(self, call: dict, ctx: dict) -> dict | Iterator:
        raise PluginError("this plugin does not serve models", "unimplemented")

    def tool(self, name: str, args: dict, ctx: dict) -> dict:
        fn = self._tools().get(name)
        if fn is None:
            raise PluginError(f"unknown tool {name}", "not_found")
        return fn(args, ctx)

    def agent(self, agent: str, input: dict, ctx: dict) -> dict:
        raise PluginError("this plugin does not run agents", "unimplemented")

    def healthy(self) -> tuple[bool, str]:
        return True, "ok"

    def _tools(self) -> dict:
        return {m._uar_tool[0]: m for m in (getattr(self, n) for n in dir(self)) if hasattr(m, "_uar_tool")}


def _final(d: dict) -> pb.PluginExecuteResponse:
    return pb.PluginExecuteResponse(text=d.get("text", ""), output=_struct(d.get("output") or
                                                                           ({"tool_calls": d["tool_calls"]}
                                                                            if d.get("tool_calls") else None)),
                                    finish_reason=d.get("finish_reason", "stop"),
                                    input_tokens=int(d.get("input_tokens", 0)),
                                    output_tokens=int(d.get("output_tokens", 0)))


class _Servicer(pbg.PluginServicer):
    def __init__(self, plugin: Plugin, stop: threading.Event):
        self.p, self.stop = plugin, stop

    def Describe(self, request, context):
        tools = [pb.ToolDescriptor(name=n, description=d, input_schema=_struct(s), side_effect=se)
                 for n, d, s, se in (m._uar_tool for m in self.p._tools().values())]
        return pb.PluginDescriptor(id=self.p.id, version=self.p.version, kind=self.p.kind, api="uar.plugin.v1",
                                   capabilities=self.p.capabilities, models=self.p.models, tools=tools)

    def Init(self, request, context):
        try:
            self.p.init(_dict(request.config), dict(request.secrets))
            return pb.PluginInitResponse(ok=True)
        except Exception as e:  # noqa: BLE001 - report init failures to the runtime
            return pb.PluginInitResponse(ok=False, message=str(e)[:300])

    def _dispatch(self, request):
        ctx = json_format.MessageToDict(request.ctx, preserving_proto_field_name=True)
        which = request.WhichOneof("call")
        if which == "model":
            call = {"model": request.model.model, "messages": [_dict(m) for m in request.model.messages],
                    "params": _dict(request.model.params), "tools": [_dict(t) for t in request.model.tools]}
            return self.p.model(call, ctx)
        if which == "tool":
            return self.p.tool(request.tool.tool, _dict(request.tool.args), ctx)
        if which == "agent":
            return self.p.agent(request.agent.agent, _dict(request.agent.input), ctx)
        raise PluginError("empty request", "invalid_argument")

    def Execute(self, request, context):
        try:
            res = self._dispatch(request)
            if not isinstance(res, dict):  # a generator from a streaming model handler
                text, final = [], {}
                for item in res:
                    if isinstance(item, str):
                        text.append(item)
                    else:
                        final = item
                res = {**final, "text": final.get("text") or "".join(text)}
            return _final(res)
        except PluginError as e:
            return pb.PluginExecuteResponse(is_error=True, error_code=e.code, error_message=str(e))
        except Exception as e:  # noqa: BLE001
            return pb.PluginExecuteResponse(is_error=True, error_code="internal", error_message=f"{type(e).__name__}: {e}"[:500])

    def ExecuteStream(self, request, context):
        try:
            res = self._dispatch(request)
            if isinstance(res, dict):
                if res.get("text"):
                    yield pb.PluginEvent(token=res["text"])
                yield pb.PluginEvent(final=_final(res))
                return
            text, final = [], {}
            for item in res:
                if isinstance(item, str):
                    text.append(item)
                    yield pb.PluginEvent(token=item)
                else:
                    final = item
            yield pb.PluginEvent(final=_final({**final, "text": final.get("text") or "".join(text)}))
        except PluginError as e:
            yield pb.PluginEvent(final=pb.PluginExecuteResponse(is_error=True, error_code=e.code, error_message=str(e)))

    def Health(self, request, context):
        ok, msg = self.p.healthy()
        return pb.HealthResponse(serving=ok, message=msg)

    def Shutdown(self, request, context):
        threading.Timer(0.2, self.stop.set).start()
        return pb.ShutdownResponse()


def serve(plugin: Plugin, address: str | None = None, workers: int = 8) -> None:
    address = address or os.environ.get("UAR_PLUGIN_ADDR", "127.0.0.1:50061")
    stop = threading.Event()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=workers))
    pbg.add_PluginServicer_to_server(_Servicer(plugin, stop), server)
    server.add_insecure_port(address)
    server.start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):
            pass
    print(f"{plugin.id}@{plugin.version} listening on {address}", file=sys.stderr, flush=True)
    stop.wait()
    server.stop(1)
