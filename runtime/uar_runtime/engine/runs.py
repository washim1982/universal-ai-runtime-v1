"""Agent registration and run lifecycle operations exposed by the API."""
from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import jsonschema

from ..config import Settings
from ..errors import UARError, not_found
from ..governance import Audit, Principal
from ..observability import current_traceparent
from ..store import Store, dumps, jsonb
from .graph import Graph, compile_graph, digest

TERMINAL = {"succeeded", "failed", "cancelled"}


def new_run_id() -> str:
    return "run_" + secrets.token_hex(10)


def _semver_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


class RunService:
    def __init__(self, settings: Settings, store: Store, audit: Audit):
        self.s = settings
        self.store = store
        self.audit = audit
        self._graphs: dict[str, Graph] = {}
        self.on_enqueue: Callable[[], None] = lambda: None
        self.plugin_pins: Callable[[], dict] = dict  # active plugin versions, recorded on each new run

    # ------------------------------------------------------------ agents

    def compile(self, definition: dict) -> Graph:
        d = digest(definition)
        if d not in self._graphs:
            self._graphs[d] = compile_graph(definition, self.s.limits)
        return self._graphs[d]

    async def register_agent(self, p: Principal, definition: dict, request_id: str = "") -> tuple[Graph, datetime]:
        p.require("agents:register")
        g = self.compile(definition)
        await self.audit.record(p, "agents.register", f"{g.agent_id}@{g.version}", "intent", request_id=request_id,
                                details={"digest": g.digest})
        row = await self.store.fetchone(
            "INSERT INTO agents (tenant, agent_id, version, digest, definition, created_by) VALUES (%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (tenant, agent_id, version) DO NOTHING RETURNING created_at",
            p.tenant, g.agent_id, g.version, g.digest, jsonb(definition), p.subject)
        if row is None:
            existing = await self.store.fetchone(
                "SELECT digest, created_at FROM agents WHERE tenant=%s AND agent_id=%s AND version=%s",
                p.tenant, g.agent_id, g.version)
            if existing and existing["digest"] != g.digest:
                raise UARError("conflict", f"{g.agent_id}@{g.version} already exists with different content; "
                               "versions are immutable, bump the version")
            return g, existing["created_at"]
        return g, row["created_at"]

    async def load_graph(self, tenant: str, agent_id: str, version: str = "") -> Graph:
        if version:
            row = await self.store.fetchone("SELECT definition FROM agents WHERE tenant=%s AND agent_id=%s AND version=%s",
                                            tenant, agent_id, version)
        else:
            rows = await self.store.fetchall("SELECT version, definition FROM agents WHERE tenant=%s AND agent_id=%s",
                                             tenant, agent_id)
            row = max(rows, key=lambda r: _semver_key(r["version"])) if rows else None
        if row is None:
            raise not_found(f"agent {agent_id}{'@' + version if version else ''}")
        return self.compile(row["definition"])

    # ------------------------------------------------------------ runs

    async def start_run(self, p: Principal, agent_id: str, version: str, input_: dict, idempotency_key: str = "",
                        request_id: str = "") -> dict:
        p.require("runs:start")
        g = await self.load_graph(p.tenant, agent_id, version)
        if g.input_schema:
            try:
                jsonschema.validate(input_, g.input_schema)
            except jsonschema.ValidationError as e:
                raise UARError("invalid_argument", f"input does not match the agent's input schema: {e.message}",
                               details={"path": list(e.absolute_path)}) from None
        rhash = hashlib.sha256(dumps({"a": agent_id, "v": g.version, "i": input_}).encode()).hexdigest()
        if idempotency_key:
            existing = await self.store.fetchone("SELECT * FROM runs WHERE tenant=%s AND idempotency_key=%s",
                                                 p.tenant, idempotency_key)
            if existing:
                return self._check_idem(existing, rhash)
        await self.audit.record(p, "runs.start", f"{agent_id}@{g.version}", "intent", request_id=request_id,
                                details={"idempotency_key": bool(idempotency_key)})
        run_id = new_run_id()
        deadline = datetime.now(timezone.utc) + timedelta(seconds=float(g.limits["timeout_s"]))
        row = await self.store.fetchone(
            "INSERT INTO runs (run_id, tenant, agent_id, version, status, input, principal, idempotency_key, "
            "request_hash, deadline_at, traceparent, request_id, plugins) VALUES (%s,%s,%s,%s,'queued',%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (tenant, idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING RETURNING *",
            run_id, p.tenant, agent_id, g.version, jsonb(input_), jsonb(p.snapshot()), idempotency_key or None,
            rhash, deadline, current_traceparent(), request_id or None, jsonb(self.plugin_pins()))
        if row is None:  # lost an idempotency race
            existing = await self.store.fetchone("SELECT * FROM runs WHERE tenant=%s AND idempotency_key=%s",
                                                 p.tenant, idempotency_key)
            return self._check_idem(existing, rhash)
        await self.append_event(run_id, {"started": {"agent_id": agent_id}}, request_id=request_id)
        self.on_enqueue()
        return row

    @staticmethod
    def _check_idem(row: dict, rhash: str) -> dict:
        if row["request_hash"] != rhash:
            raise UARError("idempotency_mismatch", "idempotency key was already used with a different request")
        return row

    async def get_run(self, p: Principal, run_id: str) -> dict:
        p.require("runs:read")
        row = await self.store.fetchone("SELECT * FROM runs WHERE run_id=%s AND tenant=%s", run_id, p.tenant)
        if row is None:
            raise not_found(f"run {run_id}")
        return row

    async def events(self, p: Principal, run_id: str, after_seq: int = 0, limit: int = 500) -> list[dict]:
        await self.get_run(p, run_id)
        return await self.store.fetchall("SELECT seq, ts, body FROM run_events WHERE run_id=%s AND seq>%s "
                                         "ORDER BY seq LIMIT %s", run_id, after_seq, limit)

    async def append_event(self, run_id: str, body: dict, conn=None, request_id: str = "") -> int:
        sql = ("INSERT INTO run_events (run_id, seq, body) SELECT %s, COALESCE(MAX(seq),0)+1, %s FROM run_events "
               "WHERE run_id=%s RETURNING seq")
        args = (run_id, jsonb(body), run_id)
        for _ in range(5):
            try:
                if conn is not None:
                    return (await (await conn.execute(sql, args)).fetchone())["seq"]
                row = await self.store.fetchone(sql, *args)
                return row["seq"]
            except Exception as e:  # unique violation from a concurrent writer: retry
                if "duplicate key" not in str(e) or conn is not None:
                    raise
                await asyncio.sleep(0.01)
        raise UARError("internal", "could not append run event")

    async def cancel(self, p: Principal, run_id: str, reason: str = "", request_id: str = "") -> dict:
        p.require("runs:cancel")
        run = await self.get_run(p, run_id)
        if run["status"] in TERMINAL:
            return run
        await self.audit.record(p, "runs.cancel", run_id, "intent", request_id=request_id, run_id=run_id,
                                details={"reason": reason[:200]})
        async with self.store.tx() as c:
            cur = await c.execute("UPDATE runs SET status='cancelled', cancel_requested=true, updated_at=now(), "
                                  "error=%s WHERE run_id=%s AND tenant=%s AND status='queued' RETURNING run_id",
                                  (jsonb({"code": "cancelled", "message": "cancelled before start"}), run_id, p.tenant))
            if await cur.fetchone():
                await self.append_event(run_id, {"completed": {"status": "cancelled"}}, conn=c)
            else:
                await c.execute("UPDATE runs SET cancel_requested=true, updated_at=now() WHERE run_id=%s AND tenant=%s",
                                (run_id, p.tenant))
        return await self.get_run(p, run_id)

    async def resolve(self, p: Principal, run_id: str, action: str, note: str = "", request_id: str = "") -> dict:
        p.require("runs:resolve")
        run = await self.get_run(p, run_id)
        if run["status"] != "needs_attention":
            raise UARError("failed_precondition", f"run is {run['status']}, not needs_attention")
        if action not in ("mark_completed", "retry_node", "fail"):
            raise UARError("invalid_argument", "action must be mark_completed, retry_node or fail")
        await self.audit.record(p, "runs.resolve", run_id, action, request_id=request_id, run_id=run_id,
                                details={"note": note[:500]})
        cp = dict(run["checkpoint"] or {})
        pending = dict(cp.get("pending") or {})
        async with self.store.tx() as c:
            if pending.get("intent_id"):
                await c.execute("UPDATE tool_intents SET status='resolved', completed_at=now(), result=%s "
                                "WHERE intent_id=%s AND tenant=%s",
                                (jsonb({"resolution": action, "by": p.subject}), pending["intent_id"], p.tenant))
            if action == "fail":
                await c.execute("UPDATE runs SET status='failed', updated_at=now(), error=%s WHERE run_id=%s",
                                (jsonb({"code": "failed_precondition", "message": "failed by operator after an "
                                        "ambiguous tool outcome"}), run_id))
                await self.append_event(run_id, {"error": {"code": "failed_precondition",
                                                           "message": "failed by operator"}}, conn=c)
            else:
                pending["resolution"] = "completed" if action == "mark_completed" else "retry"
                cp["pending"] = pending
                await c.execute("UPDATE runs SET status='queued', checkpoint=%s, lease_owner=NULL, "
                                "lease_expires_at=NULL, updated_at=now() WHERE run_id=%s", (jsonb(cp), run_id))
        self.on_enqueue()
        return await self.get_run(p, run_id)

    async def wait(self, p: Principal, run_id: str, timeout_s: float) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            run = await self.get_run(p, run_id)
            if run["status"] in TERMINAL or run["status"] == "needs_attention":
                return run
            if asyncio.get_running_loop().time() > deadline:
                raise UARError("deadline_exceeded", f"run {run_id} still {run['status']}",
                               details={"run_id": run_id})
            await asyncio.sleep(0.2)


def run_view(row: dict) -> dict[str, Any]:
    """Public JSON shape of a run (uar.v1.Run)."""
    out = {"run_id": row["run_id"], "agent_id": row["agent_id"], "version": row["version"], "status": row["status"],
           "steps": row["steps"], "current_node": row["current_node"] or "",
           "created_at": row["created_at"].isoformat().replace("+00:00", "Z"),
           "updated_at": row["updated_at"].isoformat().replace("+00:00", "Z"),
           "parent_run_id": row["parent_run_id"] or "", "cancel_requested": row["cancel_requested"]}
    if row.get("output") is not None:
        out["output"] = row["output"]
    if row.get("error"):
        out["error"] = row["error"]
    if row.get("usage"):
        out["usage"] = row["usage"]
    return out
