"""Wire activated plugins into the router (model), tool catalog (tool) and engine (agent)."""
from __future__ import annotations

import logging
from typing import AsyncIterator

from uarpb.plugin.v1 import plugin_pb2 as ppb

from ..config import ProviderCfg
from ..errors import UARError
from ..governance import Principal
from ..mcp.orchestrator import ToolInfo
from ..router.adapters.base import Adapter, ChatRequest, ChatResult
from ..router.service import Breaker
from .host import Instance, PluginManager, from_struct, to_struct

log = logging.getLogger("uar.plugins")


class PluginModelAdapter(Adapter):
    """A router provider backed by a model plugin; the version is chosen per call (run pins)."""

    def __init__(self, manager: PluginManager, plugin_id: str, cfg: ProviderCfg):
        self.id = cfg.id
        self.manager = manager
        self.plugin_id = plugin_id
        self.capabilities = set(cfg.capabilities)

    async def _inst(self) -> Instance:
        return await self.manager.instance(self.plugin_id, self.manager.version_for(self.plugin_id))

    def _request(self, req: ChatRequest) -> ppb.PluginExecuteRequest:
        call = ppb.ModelCall(model=req.model, messages=[to_struct(m) for m in req.messages],
                             params=to_struct({k: v for k, v in {"temperature": req.temperature,
                                                                 "max_tokens": req.max_tokens,
                                                                 "response_schema": req.response_schema}.items()
                                               if v is not None}),
                             tools=[to_struct({"name": t.name, "description": t.description,
                                               "input_schema": t.input_schema}) for t in req.tools])
        return ppb.PluginExecuteRequest(ctx=self.manager.execution_context(), model=call)

    @staticmethod
    def _result(r: ppb.PluginExecuteResponse, model: str) -> ChatResult:
        if r.is_error:
            raise UARError("provider_error", f"model plugin error: {r.error_message or r.error_code}", retryable=False)
        out = from_struct(r.output)
        return ChatResult(r.text, out.get("tool_calls", []), r.finish_reason or "stop",
                          r.input_tokens or None, r.output_tokens or None, model)

    async def chat(self, req: ChatRequest) -> ChatResult:
        inst = await self._inst()
        return self._result(await inst.call("Execute", self._request(req)), req.model)

    async def stream(self, req: ChatRequest) -> AsyncIterator[str | ChatResult]:
        inst = await self._inst()
        call = await inst.call("ExecuteStream", self._request(req), stream=True)
        final = None
        async for ev in call:
            if ev.WhichOneof("body") == "token":
                yield ev.token
            else:
                final = self._result(ev.final, req.model)
        if final is None:
            raise UARError("provider_error", "model plugin stream ended without a final result")
        yield final

    async def list_models(self) -> list[str]:
        inst = await self._inst()
        return list(inst.descriptor.models) if inst.descriptor else []


def install(svc) -> None:
    """Register the integration callbacks on the service's plugin manager."""
    manager: PluginManager = svc.plugins

    async def integrate(inst: Instance) -> None:
        spec = inst.manifest["spec"]
        kind = spec["type"]
        if kind == "model":
            prov = spec["provider"]
            pid, cls = prov["id"], prov["model_class"]
            s = svc.s
            cfg = next((p for p in s.providers if p.id == pid), None)
            if cfg is None:
                cfg = ProviderCfg(id=pid, type="plugin", model_class=cls,
                                  capabilities=prov.get("capabilities", ["chat", "stream"]))
                s.providers.append(cfg)
            s.router.classes.setdefault(cls, [])
            if pid not in s.router.classes[cls]:
                s.router.classes[cls].append(pid)
            svc.router.adapters[pid] = PluginModelAdapter(manager, inst.plugin_id, cfg)
            b = s.router.circuit_breaker
            svc.router.breakers.setdefault(pid, Breaker(b.failure_threshold, b.window_s, b.cooldown_s))
            svc.router.catalog[pid] = svc.router._norm(list(inst.descriptor.models))
        elif kind == "tool":
            ns = spec["tool_namespace"]
            overrides = spec.get("tools") or {}
            tools = {}
            for t in inst.descriptor.tools:
                ov = overrides.get(t.name) or {}
                tools[f"{ns}.{t.name}"] = ToolInfo(
                    f"{ns}.{t.name}", f"plugin:{inst.plugin_id}", t.name, t.description[:2000],
                    from_struct(t.input_schema) or {"type": "object"},
                    # Side effects come from the administrator's manifest; undeclared tools are treated as writes.
                    ov.get("side_effect", "write"), float(ov.get("timeout_s", inst.timeout_s)))
            svc.orch.plugin_tools[inst.plugin_id] = tools
        elif kind == "agent":
            if spec.get("graph"):
                for t in svc.s.auth.tenants:
                    sysp = Principal(t.id, "system:plugins", ("admin",), "", svc.auth.permissions(("admin",)))
                    await svc.runs.register_agent(sysp, spec["graph"])
            if spec.get("agent_id"):
                svc.plugin_agents[spec["agent_id"]] = inst.plugin_id

    manager.integrations.append(integrate)

    async def call_tool(plugin_id: str, remote_name: str, args: dict, timeout_s: float):
        inst = await manager.instance(plugin_id, manager.version_for(plugin_id))
        req = ppb.PluginExecuteRequest(ctx=manager.execution_context(), tool=ppb.ToolCall(tool=remote_name,
                                                                                           args=to_struct(args)))
        return await inst.call("Execute", req)

    svc.orch.plugin_call = call_tool

    async def call_agent(plugin_id: str, agent: str, input_: dict, run: dict) -> dict:
        inst = await manager.instance(plugin_id, manager.version_for(plugin_id))
        req = ppb.PluginExecuteRequest(
            ctx=manager.execution_context(run.get("request_id") or "", run["run_id"], run["tenant"]),
            agent=ppb.AgentCall(agent=agent, input=to_struct(input_)))
        r = await inst.call("Execute", req)
        if r.is_error:
            raise UARError("tool_error", f"agent plugin {plugin_id}: {r.error_message or r.error_code}", retryable=False)
        return from_struct(r.output) if r.output.fields else {"text": r.text}

    svc.engine.call_plugin_agent = call_agent
