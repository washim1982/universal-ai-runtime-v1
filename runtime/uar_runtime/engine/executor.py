"""Durable agent execution: leased workers, per-node checkpoints, fencing and recovery.

Checkpoint commits are atomic with their events and fenced by `lease_version`: a worker that lost
its lease can never commit. Write/external tool calls follow the intent protocol:
  1. commit checkpoint.pending = {node, intent_id}      (before anything external)
  2. orchestrator inserts the intent row, calls the tool, marks the intent completed + stores outcome
  3. commit the node output (clears pending)
On resume: pending with no intent row -> the call never started -> safe to run;
intent completed -> reuse the stored outcome; intent still open -> ambiguous -> needs_attention.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import jsonschema
from opentelemetry import context as otel_context

from ..config import Settings
from ..errors import UARError
from ..governance import Authenticator, Principal, matches_any
from ..mcp.orchestrator import CallContext, Orchestrator, args_hash
from ..observability import (NODE_SECONDS, RUN_QUEUE_SECONDS, RUNS, context_from_traceparent, get_tracer)
from ..router.adapters.base import ChatRequest, extract_json
from ..router.service import ModelRouter
from ..store import Store, jsonb
from . import cel
from .graph import Graph
from .runs import RunService, new_run_id

log = logging.getLogger("uar.engine")
MAX_CRASH_ATTEMPTS = 5


class LeaseLost(Exception):
    pass


class NodeFailure(Exception):
    def __init__(self, err: UARError, node_id: str):
        super().__init__(err.message)
        self.err, self.node_id = err, node_id


class NeedsAttention(Exception):
    def __init__(self, node_id: str, intent_id: str, message: str):
        super().__init__(message)
        self.node_id, self.intent_id = node_id, intent_id


class RunCancelled(Exception):
    pass


def _initial_state(g: Graph, perm_chain: list[list[str]] | None = None) -> dict:
    return {"next": g.start, "nodes": {}, "memory": {}, "loops": {}, "steps": 0, "tool_calls": 0,
            "tokens": {"input": 0, "output": 0}, "cost": "0", "cost_unknown": False, "estimated": False,
            "pending": None, "perm_chain": perm_chain or []}


class Engine:
    def __init__(self, settings: Settings, store: Store, runs: RunService, router: ModelRouter,
                 orchestrator: Orchestrator, auth: Authenticator):
        self.s = settings
        self.store = store
        self.runs = runs
        self.router = router
        self.orch = orchestrator
        self.auth = auth
        self.worker_id = f"w-{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(3)}"
        self._wake = asyncio.Event()
        self._tasks: dict[str, asyncio.Task] = {}
        self._stopping = False
        runs.on_enqueue = self.wake
        # Test hook: raise at named points to simulate crashes ("after_intent", "after_tool", "between_nodes").
        self.crash_at: set[str] = set()

    def wake(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------ worker loop

    async def run_forever(self) -> None:
        cfg = self.s.worker
        while not self._stopping:
            try:
                while len(self._tasks) < cfg.concurrency:
                    row = await self.claim()
                    if row is None:
                        break
                    t = asyncio.create_task(self._execute_claimed(row), name=f"run-{row['run_id']}")
                    self._tasks[row["run_id"]] = t
                    t.add_done_callback(lambda _t, rid=row["run_id"]: self._tasks.pop(rid, None))
            except Exception as e:
                log.error("worker loop error: %s", type(e).__name__)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), cfg.poll_s)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        for t in list(self._tasks.values()):
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def claim(self) -> dict | None:
        return await self.store.fetchone(
            "UPDATE runs SET status='running', lease_owner=%s, lease_expires_at=now() + make_interval(secs => %s), "
            "lease_version=lease_version+1, attempts=attempts+1, updated_at=now(), started_at=COALESCE(started_at, now()) "
            "WHERE run_id = (SELECT run_id FROM runs WHERE parent_run_id IS NULL AND "
            "(status='queued' OR (status='running' AND lease_expires_at < now())) "
            "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *",
            self.worker_id, self.s.worker.lease_s)

    async def _heartbeat(self, run_id: str, fence: int, task: asyncio.Task) -> None:
        while True:
            await asyncio.sleep(self.s.worker.heartbeat_s)
            n = await self.store.execute(
                "UPDATE runs SET lease_expires_at=now() + make_interval(secs => %s) WHERE run_id=%s AND lease_version=%s",
                self.s.worker.lease_s, run_id, fence)
            if n == 0:
                log.warning("lease lost for %s", run_id)
                task.cancel()
                return

    async def _execute_claimed(self, row: dict) -> None:
        if row["attempts"] == 1:
            RUN_QUEUE_SECONDS.observe((datetime.now(timezone.utc) - row["created_at"]).total_seconds())
        task = asyncio.current_task()
        assert task is not None
        hb = asyncio.create_task(self._heartbeat(row["run_id"], row["lease_version"], task))
        token = None
        parent_ctx = context_from_traceparent(row.get("traceparent"))
        if parent_ctx is not None:
            token = otel_context.attach(parent_ctx)
        try:
            with get_tracer().start_as_current_span("agent.run") as span:
                span.set_attributes({"uar.run_id": row["run_id"], "uar.agent": row["agent_id"],
                                     "uar.attempt": row["attempts"]})
                await self.execute(row, row["lease_version"])
        except (LeaseLost, asyncio.CancelledError):
            log.info("run %s released (lease lost or shutdown)", row["run_id"])
        except Exception:
            log.exception("run %s crashed", row["run_id"])
        finally:
            hb.cancel()
            if token is not None:
                otel_context.detach(token)

    # ------------------------------------------------------------ run execution

    def principal(self, snap: dict) -> Principal:
        key_id = snap.get("key_id", "")
        if key_id and key_id != "jwt" and key_id not in self.auth.keys:
            raise UARError("unauthenticated", "the credential that started this run has been revoked")
        roles = tuple(snap.get("roles", []))
        return Principal(snap["tenant"], snap["subject"], roles, key_id, self.auth.permissions(roles))

    async def execute(self, run: dict, fence: int) -> dict:
        """Execute (or resume) a run to a terminal or attention state. Returns the final state."""
        rid = run["run_id"]
        try:
            if run["attempts"] > MAX_CRASH_ATTEMPTS:
                raise UARError("internal", f"run abandoned after {MAX_CRASH_ATTEMPTS} worker attempts")
            p = self.principal(run["principal"])
            g = await self.runs.load_graph(run["tenant"], run["agent_id"], run["version"])
            st = run["checkpoint"] or _initial_state(g)
            if run["cancel_requested"]:
                raise RunCancelled()
            if st.get("pending"):
                await self._resume_pending(run, fence, p, g, st)
            return await self._loop(run, fence, p, g, st)
        except RunCancelled:
            return await self._finalize(run, fence, "cancelled", None,
                                        UARError("cancelled", "run cancelled; completed external actions are not "
                                                 "reversed"), None)
        except NeedsAttention as e:
            await self._finalize(run, fence, "needs_attention", None,
                                 UARError("failed_precondition", e.args[0],
                                          details={"node": e.node_id, "intent_id": e.intent_id}),
                                 None, keep_checkpoint=True)
            return {"status": "needs_attention"}
        except NodeFailure as e:
            return await self._finalize(run, fence, "failed", None, e.err, None, node=e.node_id)
        except UARError as e:
            return await self._finalize(run, fence, "failed", None, e, None)

    async def _loop(self, run: dict, fence: int, p: Principal, g: Graph, st: dict) -> dict:
        rid = run["run_id"]
        tenant = self.auth.tenant(p.tenant)
        while True:
            await self._check_run(run, g, st)
            nid = st["next"]
            node = g.nodes[nid]
            await self._event(rid, fence, {"node_started": {"node_id": nid, "node_type": node["type"],
                                                             "attempt": 1}})
            t0 = time.monotonic()
            with get_tracer().start_as_current_span(f"node {nid}") as span:
                span.set_attributes({"uar.node": nid, "uar.node_type": node["type"]})
                try:
                    output, nxt, events = await self._watched(
                        run, self._run_node(run, fence, p, tenant, g, st, nid, node))
                except (UARError, RunCancelled) as e:
                    NODE_SECONDS.labels(node["type"], "error").observe(time.monotonic() - t0)
                    await self._check_ambiguous(p, st, nid, e)
                    if isinstance(e, RunCancelled):
                        raise
                    raise NodeFailure(e, nid) from None
            dur = int((time.monotonic() - t0) * 1000)
            NODE_SECONDS.labels(node["type"], "ok").observe(time.monotonic() - t0)
            st["steps"] += 1
            if node["type"] != "condition":
                st["nodes"][nid] = {"output": output}
            st["pending"] = None
            if node["type"] == "return":
                final = output if isinstance(output, dict) else {"value": output}
                if g.output_schema:
                    try:
                        jsonschema.validate(final, g.output_schema)
                    except jsonschema.ValidationError as e:
                        raise NodeFailure(UARError("invalid_argument", f"output does not match the agent's "
                                                   f"output schema: {e.message}"), nid) from None
                events.append({"node_completed": {"node_id": nid, "outcome": "ok", "duration_ms": dur}})
                return await self._finalize(run, fence, "succeeded", final, None, st, events=events)
            st["next"] = nxt
            events.append({"node_completed": {"node_id": nid, "outcome": "ok", "duration_ms": dur, "next": nxt}})
            await self._commit(rid, fence, st, events)
            if "between_nodes" in self.crash_at:
                raise LeaseLost("simulated crash between nodes")

    async def _cancel_requested(self, run: dict) -> bool:
        row = await self.store.fetchone("SELECT bool_or(cancel_requested) AS c FROM runs WHERE run_id = ANY(%s)",
                                        [r for r in (run["run_id"], run.get("parent_run_id")) if r])
        return bool(row and row["c"])

    async def _watched(self, run: dict, coro):
        """Run a node, cancelling it if the run (or its parent) is cancelled meanwhile."""
        task = asyncio.ensure_future(coro)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=0.5)
                if done:
                    return task.result()
                if await self._cancel_requested(run):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise RunCancelled()
        finally:
            if not task.done():
                task.cancel()

    async def _check_ambiguous(self, p: Principal, st: dict, nid: str, e: Exception) -> None:
        """A write whose intent was recorded but never completed has an unknown outcome."""
        pend = st.get("pending")
        if not pend or pend.get("node") != nid or pend.get("kind") != "tool":
            return
        intent = await self.store.fetchone("SELECT status, tool FROM tool_intents WHERE intent_id=%s AND tenant=%s",
                                           pend["intent_id"], p.tenant)
        if intent and intent["status"] == "intent":
            what = "was cancelled" if isinstance(e, RunCancelled) else f"failed ({getattr(e, 'code', 'error')})"
            raise NeedsAttention(nid, pend["intent_id"], f"{intent['tool']} {what} after dispatch; its outcome is "
                                 "unknown. Resolve with mark_completed, retry_node or fail")

    async def _check_run(self, run: dict, g: Graph, st: dict) -> None:
        if await self._cancel_requested(run):
            raise RunCancelled()
        if st["steps"] >= g.limits["max_steps"]:
            raise NodeFailure(UARError("limit_exceeded", f"max_steps {g.limits['max_steps']} reached"), st["next"])
        if run.get("deadline_at") and datetime.now(timezone.utc) > run["deadline_at"]:
            raise NodeFailure(UARError("deadline_exceeded", "run timeout reached"), st["next"])

    def _ctx(self, run: dict, st: dict) -> dict:
        return {"input": run["input"], "nodes": st["nodes"], "memory": st["memory"],
                "loop": {k: {"iteration": v} for k, v in st["loops"].items()},
                "run": {"id": run["run_id"], "agent_id": run["agent_id"]}}

    def _next(self, g: Graph, nid: str, ctx: dict) -> str:
        for e in g.edges[nid]:
            if "when" not in e or cel.evaluate(e["when"].strip()[2:-1], ctx) is True:
                return e["to"]
        raise UARError("failed_precondition", f"{nid}: no outgoing edge matched")

    async def _run_node(self, run, fence, p, tenant, g, st, nid, node) -> tuple[Any, str | None, list[dict]]:
        t = node["type"]
        events: list[dict] = []
        if t == "loop":
            count = st["loops"].get(nid, 0)
            ctx = self._ctx(run, st)
            cond = True if not node.get("while") else cel.evaluate(node["while"].strip()[2:-1], ctx) is True
            if cond and count < node["max_iterations"]:
                st["loops"][nid] = count + 1
                return {"iteration": count + 1, "exhausted": False}, node["body"], events
            out = {"iteration": count, "exhausted": cond}
            st["loops"][nid] = 0
            st["nodes"][nid] = {"output": out}
            return out, self._next(g, nid, self._ctx(run, st)), events
        if t == "condition":
            return None, self._next(g, nid, self._ctx(run, st)), events
        if t == "parallel":
            output = await self._parallel(run, fence, p, tenant, g, st, nid, node, events)
        else:
            output = await self._with_retry(node, lambda: self._single(run, fence, p, tenant, g, st, nid, node,
                                                                       events))
        st["nodes"][nid] = {"output": output}
        nxt = None if t == "return" else self._next(g, nid, self._ctx(run, st))
        return output, nxt, events

    async def _with_retry(self, node: dict, fn):
        retry = node.get("retry") or {}
        attempts, backoff = int(retry.get("max_attempts", 1)), float(retry.get("backoff_s", 0.5))
        timeout = node.get("timeout_s")
        for i in range(attempts):
            try:
                return await (asyncio.wait_for(fn(), timeout) if timeout else fn())
            except asyncio.TimeoutError:
                err = UARError("deadline_exceeded", f"node {node['id']} timed out after {timeout}s")
            except UARError as e:
                err = e
            if not err.retryable or i == attempts - 1 or getattr(err, "_no_retry", False):
                raise err
            await asyncio.sleep(backoff * (2 ** i))
        raise UARError("internal", "unreachable")

    async def _single(self, run, fence, p, tenant, g, st, nid, node, events) -> Any:
        t = node["type"]
        ctx = self._ctx(run, st)
        if t == "transform":
            for k, v in (node.get("set") or {}).items():
                st["memory"][k] = cel.render_mapping(v, ctx)
            return cel.render_mapping(node.get("value"), self._ctx(run, st))
        if t == "return":
            return cel.render_mapping(node.get("value"), ctx)
        if t == "llm":
            return await self._llm(run, p, tenant, g, st, nid, node, ctx)
        if t == "tool":
            return await self._tool(run, fence, p, g, st, nid, node, ctx, events)
        if t == "agent":
            return await self._agent(run, fence, p, g, st, nid, node, ctx)
        raise UARError("invalid_graph", f"unsupported node type {t}")

    # ------------------------------------------------------------ node types

    async def _llm(self, run, p, tenant, g, st, nid, node, ctx) -> Any:
        model = node["model"]
        if not matches_any(model, g.permissions.get("models", [])):
            raise UARError("policy_denied", f"model {model} not permitted for this agent")
        used = st["tokens"]["input"] + st["tokens"]["output"]
        if used >= g.limits["max_tokens"]:
            raise UARError("limit_exceeded", f"max_tokens {g.limits['max_tokens']} reached")
        schema = node.get("output_schema")
        rctx = self.router.context(p, tenant, "", ("json",) if schema else ())
        route = self.router.route(model, rctx)
        if g.limits.get("max_cost_usd") is not None and self.router.price(route.provider, route.model, 0, 0) is None:
            raise UARError("budget_exceeded", f"cannot enforce max_cost_usd: no price is configured for "
                           f"{route.provider}/{route.model}")
        system = cel.render_template(node.get("system", ""), ctx)
        if schema:
            system = (system + "\n\n" if system else "") + (
                "Respond with only a JSON value that matches this JSON Schema, with no other text:\n"
                + __import__("json").dumps(schema))
        msgs = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": cel.render_template(node["prompt"], ctx)}]
        params = node.get("params") or {}
        max_tokens = min(params.get("max_tokens") or self.s.router.default_max_tokens, g.limits["max_tokens"] - used)
        req = ChatRequest(model=route.model, messages=msgs, temperature=params.get("temperature"),
                          max_tokens=max_tokens, response_schema=schema)
        res, route, usage = await self.router.chat(p, tenant, route, req, rctx, run.get("request_id") or "",
                                                   run["run_id"])
        st["tokens"]["input"] += usage.input_tokens
        st["tokens"]["output"] += usage.output_tokens
        st["estimated"] = st["estimated"] or usage.estimated
        if usage.cost is None:
            st["cost_unknown"] = True
        else:
            st["cost"] = str(Decimal(st["cost"]) + usage.cost)
            cap = g.limits.get("max_cost_usd")
            if cap is not None and Decimal(st["cost"]) > Decimal(str(cap)):
                raise UARError("budget_exceeded", f"max_cost_usd {cap} exceeded")
        if res.finish_reason == "refusal":
            raise UARError("invalid_model_output", "model refused the request", retryable=False)
        if schema:
            value = extract_json(res.content)
            try:
                jsonschema.validate(value, schema)
            except jsonschema.ValidationError as e:
                raise UARError("invalid_model_output", f"model output does not match output_schema: {e.message}")
            return value
        return {"text": res.content, "finish_reason": res.finish_reason}

    async def _tool(self, run, fence, p, g, st, nid, node, ctx, events, *, allow_write: bool = True) -> Any:
        name = node["tool"]
        args = cel.render_mapping(node.get("args") or {}, ctx)
        if not isinstance(args, dict):
            raise UARError("invalid_argument", f"{nid}: args must evaluate to an object")
        if st["tool_calls"] >= g.limits["max_tool_calls"]:
            raise UARError("limit_exceeded", f"max_tool_calls {g.limits['max_tool_calls']} reached")
        info = self.orch.tool(name)
        chain = [c for c in st.get("perm_chain", [])]
        agent_tools = g.permissions.get("tools", [])
        if not all(matches_any(name, pats) for pats in chain):
            raise UARError("policy_denied", f"{name} is not permitted by a parent agent")
        cctx = CallContext(run.get("request_id") or "", run["run_id"], nid, agent_tools)
        if info.side_effect != "read":
            if not allow_write:
                raise UARError("invalid_graph", f"{nid}: {info.side_effect} tools are not allowed in parallel branches")
            self.orch.authorize(p, info, args, cctx)  # deny before recording anything durable
            cctx.intent_id = "ti_" + secrets.token_hex(10)
            st["pending"] = {"node": nid, "kind": "tool", "intent_id": cctx.intent_id,
                             "args_hash": args_hash(name, args)}
            await self._commit(run["run_id"], fence, st, [])
        call_id = f"{nid}:{st['steps']}"
        events.append({"tool_call": {"id": call_id, "tool": name, "args": {k: "…" for k in args}, "node_id": nid}})
        await self._event(run["run_id"], fence, events.pop())
        if "after_intent" in self.crash_at and info.side_effect != "read":
            await self.orch.record_intent(p, cctx.intent_id, info, args_hash(name, args), cctx)
            raise LeaseLost("simulated crash after intent")
        try:
            outcome = await self.orch.execute(p, name, args, cctx)
        except UARError as e:
            if info.side_effect != "read":
                e.retryable = False  # never blindly retry a write
            raise
        st["tool_calls"] += 1
        if "after_tool" in self.crash_at and info.side_effect != "read":
            raise LeaseLost("simulated crash after tool")
        events.append({"tool_result": {"id": call_id, "tool": name, "is_error": outcome.is_error,
                                       "summary": outcome.text()[:200], "node_id": nid}})
        if outcome.is_error:
            err = UARError("tool_error", f"{name} returned an error: {outcome.text()[:300]}",
                           retryable=info.side_effect == "read")
            raise err
        return outcome.structured if outcome.structured is not None else {"text": outcome.text()}

    async def _agent(self, run, fence, p, g, st, nid, node, ctx) -> Any:
        child_id = node["agent"]
        depth = run.get("depth", 0) + 1
        if depth > g.limits["max_depth"]:
            raise UARError("limit_exceeded", f"max_depth {g.limits['max_depth']} reached")
        cg = await self.runs.load_graph(run["tenant"], child_id, node.get("version", ""))
        child_input = cel.render_mapping(node.get("input") or {}, ctx)
        if cg.input_schema:
            try:
                jsonschema.validate(child_input, cg.input_schema)
            except jsonschema.ValidationError as e:
                raise UARError("invalid_argument", f"sub-agent input invalid: {e.message}") from None
        child_run_id = new_run_id()
        chain = list(st.get("perm_chain", [])) + [g.permissions.get("tools", [])]
        cstate = _initial_state(cg, chain)
        st["pending"] = {"node": nid, "kind": "agent", "child_run_id": child_run_id}
        async with self.store.tx() as c:
            await c.execute(
                "INSERT INTO runs (run_id, tenant, agent_id, version, status, input, principal, parent_run_id, depth, "
                "checkpoint, deadline_at, lease_owner, lease_version, attempts, traceparent, request_id) VALUES "
                "(%s,%s,%s,%s,'running',%s,%s,%s,%s,%s,%s,%s,0,1,%s,%s)",
                (child_run_id, run["tenant"], cg.agent_id, cg.version, jsonb(child_input), jsonb(run["principal"]),
                 run["run_id"], depth, jsonb(cstate), run.get("deadline_at"), self.worker_id, run.get("traceparent"),
                 run.get("request_id")))
            await self._commit(run["run_id"], fence, st, [], conn=c)
        return await self._run_child(child_run_id, run, nid)

    async def _run_child(self, child_run_id: str, parent: dict, nid: str) -> Any:
        row = await self.store.fetchone(
            "UPDATE runs SET lease_owner=%s, lease_version=lease_version+1 WHERE run_id=%s RETURNING *",
            self.worker_id, child_run_id)
        if row["status"] not in ("running", "queued"):
            result = row
        else:
            await self.execute(row, row["lease_version"])
            result = await self.store.fetchone("SELECT * FROM runs WHERE run_id=%s", child_run_id)
        if result["status"] == "succeeded":
            return result["output"]
        if result["status"] == "needs_attention":
            raise NeedsAttention(nid, "", f"sub-agent run {child_run_id} needs attention; resolve it, then resolve "
                                 "this run with retry_node")
        err = result.get("error") or {}
        raise UARError(err.get("code", "internal") if err.get("code") != "cancelled" else "failed_precondition",
                       f"sub-agent {result['agent_id']} {result['status']}: {err.get('message', '')}",
                       retryable=False, details={"child_run_id": child_run_id})

    async def _parallel(self, run, fence, p, tenant, g, st, nid, node, events) -> dict:
        sem = asyncio.Semaphore(int(node.get("max_concurrency", 4)))
        ctx = self._ctx(run, st)

        async def branch(name: str, bn: dict):
            bnode = {**bn, "id": f"{nid}_{name}"}
            async with sem:
                if bn["type"] == "llm":
                    return name, await self._llm(run, p, tenant, g, st, nid, bnode, ctx)
                if bn["type"] == "tool":
                    return name, await self._tool(run, fence, p, g, st, nid, bnode, ctx, events, allow_write=False)
                return name, cel.render_mapping(bn.get("value"), ctx)
        results = await asyncio.gather(*(branch(k, v) for k, v in node["branches"].items()))
        return dict(results)

    # ------------------------------------------------------------ resume & persistence

    async def _resume_pending(self, run, fence, p, g, st) -> None:
        pend = st["pending"]
        nid = pend["node"]
        if pend["kind"] == "agent":
            output = await self._run_child(pend["child_run_id"], run, nid)
            st["nodes"][nid] = {"output": output}
        elif pend["kind"] == "tool":
            resolution = pend.get("resolution")
            intent = await self.store.fetchone("SELECT * FROM tool_intents WHERE intent_id=%s AND tenant=%s",
                                               pend["intent_id"], p.tenant)
            if resolution == "retry" or (intent is None and resolution is None):
                st["pending"] = None   # the call never started, or an operator chose to retry
                return
            if resolution == "completed":
                st["nodes"][nid] = {"output": {"resolved": "mark_completed"}}
            elif intent["status"] == "completed":
                res = intent["result"] or {}
                st["nodes"][nid] = {"output": res.get("structured") if res.get("structured") is not None
                                    else {"text": "\n".join(c.get("text", "") for c in res.get("content", []))}}
                st["tool_calls"] += 1
            elif intent["status"] == "failed":
                raise NodeFailure(UARError("tool_error", f"{intent['tool']} failed before the worker stopped",
                                           retryable=False), nid)
            else:
                raise NeedsAttention(nid, pend["intent_id"],
                                     f"{intent['tool']} may or may not have completed before the worker stopped; "
                                     "resolve with mark_completed, retry_node or fail")
        st["steps"] += 1
        st["pending"] = None
        st["next"] = None if g.nodes[nid]["type"] == "return" else self._next(g, nid, self._ctx(run, st))
        await self._commit(run["run_id"], fence, st, [{"node_completed": {"node_id": nid, "outcome": "recovered",
                                                                           "next": st["next"] or ""}}])

    def _usage(self, st: dict | None) -> dict:
        if not st:
            return {}
        u = {"input_tokens": st["tokens"]["input"], "output_tokens": st["tokens"]["output"],
             "estimated": st.get("estimated", False)}
        if not st.get("cost_unknown"):
            u["cost"] = {"amount": st["cost"], "currency": self.s.pricing.currency}
        return u

    async def _commit(self, rid: str, fence: int, st: dict, events: list[dict], conn=None) -> None:
        async def do(c):
            cur = await c.execute("UPDATE runs SET checkpoint=%s, steps=%s, current_node=%s, usage=%s, updated_at=now() "
                                  "WHERE run_id=%s AND lease_version=%s",
                                  (jsonb(st), st["steps"], st.get("next"), jsonb(self._usage(st)), rid, fence))
            if cur.rowcount == 0:
                raise LeaseLost(rid)
            for e in events:
                await self.runs.append_event(rid, e, conn=c)
        if conn is not None:
            await do(conn)
        else:
            async with self.store.tx() as c:
                await do(c)

    async def _event(self, rid: str, fence: int, body: dict) -> None:
        async with self.store.tx() as c:
            cur = await c.execute("SELECT 1 FROM runs WHERE run_id=%s AND lease_version=%s FOR UPDATE", (rid, fence))
            if await cur.fetchone() is None:
                raise LeaseLost(rid)
            await self.runs.append_event(rid, body, conn=c)

    async def _finalize(self, run, fence, status, output, err: UARError | None, st, *, events=None, node=None,
                        keep_checkpoint: bool = False) -> dict:
        rid = run["run_id"]
        evs = list(events or [])
        if err is not None:
            e = err.to_dict(run.get("request_id") or "")
            if node:
                e["details"] = {**e.get("details", {}), "node": node}
            evs.append({"error": e})
        else:
            evs.append({"completed": {"status": status, "output": output}})
        async with self.store.tx() as c:
            sets = ["status=%s", "output=%s", "error=%s", "updated_at=now()", "lease_owner=NULL",
                    "lease_expires_at=NULL"]
            args: list[Any] = [status, jsonb(output), jsonb(err.to_dict(run.get("request_id") or "") if err else None)]
            if st is not None and not keep_checkpoint:
                sets += ["checkpoint=%s", "usage=%s", "steps=%s"]
                args += [jsonb(st), jsonb(self._usage(st)), st["steps"]]
            cur = await c.execute(f"UPDATE runs SET {', '.join(sets)} WHERE run_id=%s AND lease_version=%s",
                                  (*args, rid, fence))
            if cur.rowcount == 0:
                raise LeaseLost(rid)
            for ev in evs:
                await self.runs.append_event(rid, ev, conn=c)
        RUNS.labels(status).inc()
        return {"status": status, "output": output}
