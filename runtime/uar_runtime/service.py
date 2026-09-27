"""RuntimeService: the single implementation behind gRPC, HTTP/SSE and WebSocket.

Every method takes an authenticated Principal and plain JSON-shaped dicts that follow
proto/uarpb/v1/runtime.proto (transports validate against the proto before calling in).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import yaml

from .config import Settings
from .engine.executor import Engine
from .engine.graph import Graph
from .engine.runs import TERMINAL, RunService, run_view
from .errors import UARError, invalid
from .governance import Audit, Authenticator, Budget, Principal, RateLimiter
from .mcp.orchestrator import CallContext, Orchestrator, args_hash
from .observability import current_trace_id, get_tracer
from .router.adapters.base import ChatRequest, ChatResult, ToolSpec
from .router.service import ModelRouter
from .simulation.planner import SimInputs, preview_inference, simulate, static_plan
from .store import Store, dumps, jsonb

log = logging.getLogger("uar.service")
ROLES = {"system", "user", "assistant", "tool"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class RuntimeService:
    def __init__(self, settings: Settings, store: Store):
        self.s = settings
        self.store = store
        self.auth = Authenticator(settings)
        self.audit = Audit(store)
        self.budget = Budget(store)
        self.limiter = RateLimiter()
        self.router = ModelRouter(settings, store, self.audit, self.budget)
        self.orch = Orchestrator(settings, store, self.audit)
        self.runs = RunService(settings, store, self.audit)
        self.engine = Engine(settings, store, self.runs, self.router, self.orch, self.auth)
        self._worker: asyncio.Task | None = None
        self.started = False
        self.startup_status: dict[str, Any] = {}

    # ------------------------------------------------------------ lifecycle

    async def start(self, *, discover: bool = True, mcp: bool = True, worker: bool | None = None) -> None:
        await self.store.open()
        self.startup_status["migrations"] = await self.store.migrate()
        if discover:
            self.startup_status["providers"] = await self.router.refresh_catalog()
        if mcp:
            self.startup_status["mcp"] = await self.orch.start()
        if self.s.agents_dir:
            self.startup_status["agents"] = await self._register_bundled_agents(Path(self.s.agents_dir))
        if worker if worker is not None else self.s.worker.embedded:
            self._worker = asyncio.create_task(self.engine.run_forever(), name="uar-worker")
        self.started = True
        log.info("runtime started", extra={"fields": self.startup_status})

    async def stop(self) -> None:
        await self.engine.stop()
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        await self.orch.stop()
        await self.router.aclose()
        await self.store.close()

    async def _register_bundled_agents(self, folder: Path) -> list[str]:
        """Register agent YAML files for every tenant (operator-provided, like MCP servers)."""
        done = []
        for f in sorted(folder.glob("*.yaml")):
            definition = yaml.safe_load(f.read_text(encoding="utf-8"))
            for t in self.s.auth.tenants:
                sysp = Principal(t.id, "system:bootstrap", ("admin",), "", self.auth.permissions(("admin",)))
                try:
                    await self.runs.register_agent(sysp, definition)
                except UARError as e:
                    log.error("bundled agent %s not registered: %s", f.name, e.message)
                    break
            else:
                done.append(f.stem)
        return done

    async def ready(self) -> dict:
        db = await self.store.ping()
        return {"ready": db and self.started, "database": db,
                "mcp": {k: bool(v.tools) for k, v in self.orch.sessions.items()}}

    @asynccontextmanager
    async def admit(self, p: Principal):
        q = self.auth.tenant(p.tenant).quotas
        key = f"{p.tenant}/{p.subject}"
        self.limiter.acquire(key, q.requests_per_minute, q.concurrent_requests)
        try:
            yield
        finally:
            self.limiter.release(key)

    # ------------------------------------------------------------ inference

    def _messages(self, req: dict) -> list[dict]:
        msgs = []
        for m in req.get("messages") or []:
            role = m.get("role", "")
            if role not in ROLES:
                raise invalid(f"message role must be one of {sorted(ROLES)}")
            msgs.append({"role": role, "content": m.get("content", ""), "tool_calls": m.get("tool_calls") or [],
                         "tool_call_id": m.get("tool_call_id", "")})
        if not msgs:
            if not req.get("input"):
                raise invalid("provide messages or input")
            msgs = [{"role": "user", "content": req["input"]}]
        return msgs

    def _tool_specs(self, p: Principal, names: list[str], auto: bool) -> list[ToolSpec]:
        specs = []
        visible = {t.name: t for t in self.orch.visible_tools(p)}
        for n in names:
            t = visible.get(n)
            if t is None:
                self.orch.tool(n)  # raises not_found / unavailable when missing
                raise UARError("policy_denied", f"tool {n} is not available to this caller")
            specs.append(ToolSpec(n, t.description, t.input_schema))
        if auto:
            p.require("tools:execute")
        return specs

    def _chat_request(self, p: Principal, req: dict) -> tuple[ChatRequest, tuple[str, ...], bool]:
        params = req.get("params") or {}
        mode = req.get("tool_mode") or "suggest"
        if mode not in ("suggest", "auto"):
            raise invalid("tool_mode must be suggest or auto")
        tools = self._tool_specs(p, list(req.get("tools") or []), mode == "auto")
        caps = tuple(c for c, on in (("tools", bool(tools)), ("json", bool(params.get("response_schema"))),
                                     ("stream", bool(req.get("stream")))) if on)
        cr = ChatRequest(model="", messages=self._messages(req), tools=tools,
                         temperature=params.get("temperature"), max_tokens=params.get("max_tokens"),
                         top_p=params.get("top_p"), stop=list(params.get("stop") or []),
                         response_schema=params.get("response_schema"),
                         namespaced_extensions=req.get("extensions") or {})
        return cr, caps, mode == "auto"

    async def infer(self, p: Principal, req: dict, request_id: str) -> dict:
        if req.get("agent"):
            return await self._infer_agent(p, req, request_id)
        tenant = self.auth.tenant(p.tenant)
        cr, caps, auto = self._chat_request(p, req)
        ctx = self.router.context(p, tenant, req.get("data_class", ""), caps)
        route = self.router.route(req.get("model", ""), ctx)
        with get_tracer().start_as_current_span("inference") as span:
            span.set_attribute("uar.model.requested", req.get("model", ""))
            total_in = total_out = 0
            cost, estimated, unknown = 0, False, False
            steps = 0
            while True:
                res, route, u = await self.router.chat(p, tenant, route, cr, ctx, request_id)
                total_in += u.input_tokens
                total_out += u.output_tokens
                estimated |= u.estimated
                if u.cost is None:
                    unknown = True
                else:
                    cost += u.cost
                if not (auto and res.finish_reason == "tool_calls" and res.tool_calls):
                    break
                steps += 1
                if steps > self.s.limits.auto_tool_max_steps:
                    raise UARError("limit_exceeded", f"auto tool loop exceeded {self.s.limits.auto_tool_max_steps} steps")
                await self._run_tool_calls(p, cr, res, request_id)
        usage = {"input_tokens": total_in, "output_tokens": total_out, "estimated": estimated,
                 "price_version": self.s.pricing.version}
        if not unknown:
            usage["cost"] = {"amount": f"{cost:.8f}".rstrip("0").rstrip(".") or "0", "currency": self.s.pricing.currency}
        return {"request_id": request_id, "provider": route.provider, "model": route.model, "content": res.content,
                "finish_reason": res.finish_reason, "tool_calls": res.tool_calls, "usage": usage,
                "route": route.to_dict()}

    async def _run_tool_calls(self, p: Principal, cr: ChatRequest, res: ChatResult, request_id: str,
                              emit=None) -> None:
        cr.messages.append({"role": "assistant", "content": res.content, "tool_calls": res.tool_calls})
        for tc in res.tool_calls:
            if emit:
                await emit({"tool_call": {"id": tc["id"], "tool": tc["name"], "args": tc["args"]}})
            try:
                out = await self.orch.execute(p, tc["name"], tc["args"], CallContext(request_id))
                text, is_error = out.text(), out.is_error
            except UARError as e:
                if e.code in ("audit_unavailable",):
                    raise
                text, is_error = f"error: {e.code}: {e.message}", True
            if emit:
                await emit({"tool_result": {"id": tc["id"], "tool": tc["name"], "is_error": is_error,
                                            "summary": text[:200]}})
            # Tool output is untrusted data: it is passed back as a tool message, never as instructions.
            cr.messages.append({"role": "tool", "content": text, "tool_call_id": tc["id"], "name": tc["name"],
                                "is_error": is_error})

    async def infer_stream(self, p: Principal, req: dict, request_id: str) -> AsyncIterator[dict]:
        seq = 0
        trace = current_trace_id()

        def ev(body: dict, run_id: str = "") -> dict:
            nonlocal seq
            seq += 1
            return {"run_id": run_id, "seq": seq, "ts": now_iso(), "trace_id": trace, "request_id": request_id, **body}

        try:
            if req.get("agent"):
                run = await self._start_agent_run(p, req, request_id)
                async for e in self.watch_run(p, run["run_id"], 0):
                    yield e
                return
            tenant = self.auth.tenant(p.tenant)
            cr, caps, auto = self._chat_request(p, req)
            ctx = self.router.context(p, tenant, req.get("data_class", ""), caps)
            route = self.router.route(req.get("model", ""), ctx)
            steps = 0
            pending: list[dict] = []

            async def emit(body: dict) -> None:
                pending.append(body)

            first = True
            while True:
                route, it = await self.router.stream(p, tenant, route, cr, ctx, request_id)
                if first:
                    yield ev({"started": {"model": route.model, "provider": route.provider}})
                    first = False
                final: ChatResult | None = None
                usage = None
                async for item in it:
                    if isinstance(item, tuple):
                        _, final, usage = item
                    else:
                        yield ev({"token": {"text": item}})
                assert final is not None and usage is not None
                yield ev({"usage": usage.to_dict()})
                if not (auto and final.finish_reason == "tool_calls" and final.tool_calls):
                    break
                steps += 1
                if steps > self.s.limits.auto_tool_max_steps:
                    raise UARError("limit_exceeded", "auto tool loop exceeded its step limit")
                await self._run_tool_calls(p, cr, final, request_id, emit)
                for body in pending:
                    yield ev(body)
                pending.clear()
            done = {"status": "succeeded", "content": final.content, "finish_reason": final.finish_reason}
            yield ev({"completed": done})
        except UARError as e:
            yield ev({"error": e.to_dict(request_id)})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("stream failed")
            yield ev({"error": UARError("internal", "internal error").to_dict(request_id)})

    async def _start_agent_run(self, p: Principal, req: dict, request_id: str) -> dict:
        text = req.get("input") or next((m.get("content", "") for m in reversed(req.get("messages") or [])
                                         if m.get("role") == "user"), "")
        return await self.runs.start_run(p, req["agent"], "", {"prompt": text}, request_id=request_id)

    async def _infer_agent(self, p: Principal, req: dict, request_id: str) -> dict:
        run = await self._start_agent_run(p, req, request_id)
        g = await self.runs.load_graph(p.tenant, run["agent_id"], run["version"])
        run = await self.runs.wait(p, run["run_id"], float(g.limits["timeout_s"]) + 5)
        if run["status"] != "succeeded":
            err = run.get("error") or {}
            raise UARError(err.get("code", "internal"), err.get("message", f"agent run {run['status']}"),
                           retryable=False, details={"run_id": run["run_id"], "status": run["status"]})
        out = run["output"] or {}
        content = out.get("text") or out.get("content") or out.get("value")
        if not isinstance(content, str):
            content = json.dumps(out)
        return {"request_id": request_id, "provider": "", "model": "", "content": content, "finish_reason": "stop",
                "usage": run.get("usage") or {}, "run_id": run["run_id"], "output": out}

    # ------------------------------------------------------------ tools & catalog

    async def execute_tool(self, p: Principal, req: dict, request_id: str) -> dict:
        name, args = req.get("tool", ""), req.get("args") or {}
        key = req.get("idempotency_key", "")
        rhash = hashlib.sha256(f"{name}\n{dumps(args)}".encode()).hexdigest()
        if key:
            row = await self.store.fetchone(
                "INSERT INTO idempotency (tenant, operation, key, request_hash) VALUES (%s,'tool.execute',%s,%s) "
                "ON CONFLICT DO NOTHING RETURNING key", p.tenant, key, rhash)
            if row is None:
                prev = await self.store.fetchone("SELECT request_hash, response FROM idempotency WHERE tenant=%s AND "
                                                 "operation='tool.execute' AND key=%s", p.tenant, key)
                if prev["request_hash"] != rhash:
                    raise UARError("idempotency_mismatch", "idempotency key was already used with a different request")
                if prev["response"] is None:
                    raise UARError("conflict", "a request with this idempotency key is still in progress or its "
                                   "outcome is unknown", retryable=False)
                return prev["response"]
        out = await self.orch.execute(p, name, args, CallContext(request_id))
        resp = {"request_id": request_id, **out.to_dict()}
        if key:
            await self.store.execute("UPDATE idempotency SET response=%s WHERE tenant=%s AND operation='tool.execute' "
                                     "AND key=%s", jsonb(resp), p.tenant, key)
        return resp

    async def list_tools(self, p: Principal) -> dict:
        return {"tools": [t.to_dict() for t in self.orch.visible_tools(p)]}

    async def list_models(self, p: Principal) -> dict:
        p.require("models:list")
        out = []
        for alias, target in sorted(self.s.router.aliases.items()):
            pid, model = target.split("/", 1)
            cls = self.s.provider(pid).model_class
            known = self.router.catalog.get(pid)
            out.append({"name": alias, "model_class": cls, "provider": pid, "model": model,
                        "capabilities": self.s.provider(pid).capabilities,
                        "available": cls in self.s.egress.allowed_classes and known is not None and model in known})
        for pc in self.s.providers:
            if pc.model_class not in self.s.egress.allowed_classes or not p.has(f"inference:{pc.model_class}"):
                continue
            for m in sorted(self.router.catalog.get(pc.id) or []):
                if m.endswith(":latest") and m[:-7] in (self.router.catalog.get(pc.id) or set()):
                    continue
                out.append({"name": f"{pc.model_class}:{pc.id}/{m}", "model_class": pc.model_class, "provider": pc.id,
                            "model": m, "capabilities": pc.capabilities, "available": True})
        return {"models": out}

    # ------------------------------------------------------------ agents & runs

    async def register_agent(self, p: Principal, req: dict, request_id: str) -> dict:
        definition = req.get("definition")
        if not isinstance(definition, dict):
            raise invalid("definition is required")
        g, created = await self.runs.register_agent(p, definition, request_id)
        return {"agent_id": g.agent_id, "version": g.version, "digest": g.digest,
                "created_at": created.isoformat().replace("+00:00", "Z"), "warnings": g.warnings}

    async def start_run(self, p: Principal, req: dict, request_id: str) -> dict:
        row = await self.runs.start_run(p, req.get("agent_id", ""), req.get("version", ""), req.get("input") or {},
                                        req.get("idempotency_key", ""), request_id)
        return run_view(row)

    async def get_run(self, p: Principal, run_id: str) -> dict:
        return run_view(await self.runs.get_run(p, run_id))

    async def cancel_run(self, p: Principal, run_id: str, reason: str, request_id: str) -> dict:
        return run_view(await self.runs.cancel(p, run_id, reason, request_id))

    async def resolve_run(self, p: Principal, run_id: str, action: str, note: str, request_id: str) -> dict:
        return run_view(await self.runs.resolve(p, run_id, action, note, request_id))

    async def watch_run(self, p: Principal, run_id: str, after_seq: int, idle_timeout_s: float = 3600) -> AsyncIterator[dict]:
        """Yield durable run events after `after_seq` until a terminal event."""
        last = after_seq
        started = time.monotonic()
        while True:
            rows = await self.runs.events(p, run_id, last)
            for r in rows:
                last = r["seq"]
                body = r["body"]
                yield {"run_id": run_id, "seq": r["seq"], "ts": r["ts"].isoformat().replace("+00:00", "Z"), **body}
                if "completed" in body or "error" in body:
                    return
            if not rows:
                run = await self.runs.get_run(p, run_id)
                if run["status"] in TERMINAL and last >= (await self._max_seq(run_id)):
                    return
                if time.monotonic() - started > idle_timeout_s:
                    return
                await asyncio.sleep(0.2)

    async def _max_seq(self, run_id: str) -> int:
        row = await self.store.fetchone("SELECT COALESCE(MAX(seq),0) AS m FROM run_events WHERE run_id=%s", run_id)
        return row["m"]

    # ------------------------------------------------------------ dry-run

    async def dry_run(self, p: Principal, req: dict, request_id: str) -> dict:
        p.require("dryrun")
        tenant = self.auth.tenant(p.tenant)
        mode = req.get("mode") or "static"
        if mode not in ("static", "simulate"):
            raise invalid("mode must be static or simulate")
        inp = SimInputs(self.s, p, tenant, self.router.context(p, tenant),
                        {t.name: {"side_effect": t.side_effect, "input_schema": t.input_schema}
                         for t in self.orch.catalog().values()},
                        lambda aid, ver: self.runs.load_graph(p.tenant, aid, ver))
        target = "inference"
        if req.get("inference"):
            inf = req["inference"]
            caps = tuple(c for c, on in (("tools", bool(inf.get("tools"))),
                                         ("json", bool((inf.get("params") or {}).get("response_schema"))),
                                         ("stream", bool(inf.get("stream")))) if on)
            report = preview_inference(inp, inf.get("model", ""), caps, inf.get("data_class", ""))
        else:
            try:
                if req.get("agent_id"):
                    target = req["agent_id"]
                    g: Graph = await self.runs.load_graph(p.tenant, req["agent_id"], req.get("version", ""))
                elif req.get("definition"):
                    target = "inline-definition"
                    g = self.runs.compile(req["definition"])
                else:
                    raise invalid("dry-run needs agent_id, definition or inference")
            except UARError as e:
                if e.code != "invalid_graph":
                    raise
                return {"request_id": request_id, "mode": mode, "valid": False, "errors": [e.message],
                        "executed_nothing": True}
            if mode == "simulate":
                report = await simulate(inp, g, req.get("input") or {}, req.get("fixtures") or {},
                                        int(req.get("seed") or 0))
            else:
                report = await static_plan(inp, g)
        await self.audit.record(p, "dryrun", target, "succeeded", request_id=request_id, required=False,
                                details={"mode": mode})
        return {"request_id": request_id, **report.to_dict(self.s.pricing.currency)}

    async def decide_approval(self, p: Principal, req: dict, request_id: str) -> dict:
        raise UARError("unimplemented", "approvals are planned for M9")
