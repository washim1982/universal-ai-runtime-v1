"""MCP orchestrator: administrator-registered servers, sandboxed sessions, governed tool calls.

Security model
- Servers come only from operator configuration; clients can never supply commands or URLs.
- Side-effect classes come from configuration, never from server-provided annotations
  (tool descriptions and results are untrusted data).
- Every call: RBAC -> agent permissions -> tool policy (default deny) -> JSON Schema validation
  -> audit intent (fail closed) -> [write/external: durable intent row] -> call with timeout
  -> normalise and cap output -> completion records.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import jsonschema
import mcp
from mcp import StdioServerParameters

from ..config import McpServerCfg, Settings
from ..errors import UARError
from ..governance import Audit, Principal, matches_any, tool_decision, tool_visible
from ..observability import POLICY_DENIALS, TOOL_CALLS, TOOL_SECONDS, get_tracer
from ..store import Store, dumps, jsonb

log = logging.getLogger("uar.mcp")
REPO = Path(__file__).resolve().parents[3]


@dataclass
class ToolInfo:
    name: str               # namespaced: <server>.<tool>
    server: str
    remote_name: str
    description: str
    input_schema: dict
    side_effect: str
    timeout_s: float

    def to_dict(self) -> dict:
        return {"name": self.name, "server": self.server, "description": self.description,
                "input_schema": self.input_schema, "side_effect": self.side_effect}


@dataclass
class CallContext:
    request_id: str = ""
    run_id: str | None = None
    node_id: str | None = None
    agent_tools: list[str] | None = None     # agent permission patterns; None = not in an agent run
    intent_id: str | None = None             # engine-provided durable intent id


@dataclass
class ToolOutcome:
    tool: str
    is_error: bool
    content: list[dict]
    structured: Any
    truncated: bool
    duration_ms: int
    intent_id: str | None = None
    side_effect: str = "read"

    def to_dict(self) -> dict:
        d = {"tool": self.tool, "is_error": self.is_error, "content": self.content, "truncated": self.truncated,
             "untrusted": True, "duration_ms": self.duration_ms}
        if self.structured is not None:
            d["structured"] = self.structured if isinstance(self.structured, dict) else {"value": self.structured}
        return d

    def text(self) -> str:
        if self.structured is not None:
            return json.dumps(self.structured)[:20000]
        return "\n".join(c.get("text", "") for c in self.content)[:20000]


def args_hash(tool: str, args: dict) -> str:
    return hashlib.sha256(f"{tool}\n{dumps(args)}".encode()).hexdigest()


def check_url_egress(url: str, allow_private: list[str]) -> None:
    """Reject non-HTTP schemes and hosts resolving to private, loopback, link-local or reserved
    addresses unless explicitly allowed. All resolved addresses must pass (DNS-rebinding guard);
    redirects are disabled by the client."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise UARError("egress_denied", "MCP server URL must be http(s) with a host")
    nets = [ipaddress.ip_network(n, strict=False) for n in allow_private]
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except socket.gaierror as e:
        raise UARError("unavailable", f"cannot resolve MCP server host {u.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if any(ip in n for n in nets):
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast \
                or ip.is_unspecified:
            raise UARError("egress_denied", f"MCP server host resolves to a non-public address ({ip})",
                           details={"host": u.hostname})


def container_command(cfg: McpServerCfg) -> tuple[str, list[str]]:
    name = f"uar-mcp-{cfg.id}-{secrets.token_hex(3)}"
    args = ["run", "-i", "--rm", "--name", name, "--network", cfg.network, "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64",
            "--memory", cfg.memory, "--cpus", cfg.cpus, "--tmpfs", "/tmp:size=16m", "--user", "10001:10001",
            "--label", "uar.mcp=1"]
    for k, v in cfg.env.items():
        args += ["-e", f"{k}={v}"]
    for m in cfg.mounts:
        src = str((REPO / m.src).resolve()) if not os.path.isabs(m.src) else m.src
        args += ["--mount", f"type=bind,src={src},dst={m.dst}" + (",readonly" if m.readonly else "")]
    if cfg.command:
        args += ["--entrypoint", cfg.command]   # the image's own entrypoint is replaced by the server command
    args.append(cfg.image or "")
    args += cfg.args
    return "docker", args


class ServerSession:
    """Owns one MCP client connection. The connection is entered and exited inside its own task
    (anyio cancel scopes must not cross tasks); callers use the connected client concurrently."""

    def __init__(self, cfg: McpServerCfg, settings: Settings):
        self.cfg = cfg
        self.settings = settings
        self.client: mcp.Client | None = None
        self.tools: dict[str, ToolInfo] = {}
        self._task: asyncio.Task | None = None
        self._ready: asyncio.Future | None = None
        self._stop: asyncio.Event | None = None
        self._lock = asyncio.Lock()
        self.last_error: str | None = None

    def _target(self) -> Any:
        c = self.cfg
        if c.transport == "http":
            check_url_egress(c.url or "", self.settings.egress.allow_private)
            return c.url
        if c.sandbox == "container":
            cmd, args = container_command(c)
            return StdioServerParameters(command=cmd, args=args)
        cmd = sys.executable if c.command == "python" else (c.command or "")
        env = {k: (str((REPO / v[len("repo:"):]).resolve()) if v.startswith("repo:") else v) for k, v in c.env.items()}
        env.setdefault("PYTHONPATH", str(REPO / "mcp-servers"))
        return StdioServerParameters(command=cmd, args=c.args, env=env, cwd=str(REPO))

    async def _owner(self) -> None:
        assert self._ready is not None and self._stop is not None
        try:
            async with mcp.Client(self._target(), mode="auto", read_timeout_seconds=self.cfg.timeout_s) as client:
                listed = await client.list_tools()
                self.tools = {}
                for t in listed.tools:
                    ov = self.cfg.tools.get(t.name)
                    self.tools[f"{self.cfg.id}.{t.name}"] = ToolInfo(
                        f"{self.cfg.id}.{t.name}", self.cfg.id, t.name, (t.description or "")[:2000],
                        t.input_schema or {"type": "object"},
                        ov.side_effect if ov else self.cfg.default_side_effect,
                        (ov.timeout_s if ov and ov.timeout_s else self.cfg.timeout_s))
                self.client = client
                self.last_error = None
                if not self._ready.done():
                    self._ready.set_result(True)
                await self._stop.wait()
        except BaseException as e:  # noqa: BLE001 - surface any startup failure to the waiter
            self.last_error = f"{type(e).__name__}: {e}"[:300]
            if not self._ready.done():
                self._ready.set_exception(e if isinstance(e, UARError) else UARError(
                    "unavailable", f"MCP server {self.cfg.id} failed to start", details={"error": self.last_error}))
            if isinstance(e, asyncio.CancelledError):
                raise
        finally:
            self.client = None

    async def ensure(self) -> None:
        async with self._lock:
            if self.client is not None and self._task is not None and not self._task.done():
                return
            await self._shutdown_task()
            loop = asyncio.get_running_loop()
            self._ready, self._stop = loop.create_future(), asyncio.Event()
            self._task = asyncio.create_task(self._owner(), name=f"mcp-{self.cfg.id}")
            await asyncio.wait_for(asyncio.shield(self._ready), timeout=max(30.0, self.cfg.timeout_s))

    async def reset(self) -> None:
        async with self._lock:
            await self._shutdown_task()

    async def _shutdown_task(self) -> None:
        if self._task is not None and not self._task.done():
            assert self._stop is not None
            self._stop.set()
            try:
                await asyncio.wait_for(self._task, 10)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()
        self._task = None
        self.client = None

    async def call(self, remote_name: str, args: dict, timeout_s: float):
        await self.ensure()
        assert self.client is not None
        return await asyncio.wait_for(self.client.call_tool(remote_name, args), timeout_s)


class Orchestrator:
    def __init__(self, settings: Settings, store: Store, audit: Audit):
        self.s = settings
        self.store = store
        self.audit = audit
        self.sessions = {c.id: ServerSession(c, settings) for c in settings.mcp_servers}

    async def start(self, timeout: float = 60) -> dict[str, str]:
        status: dict[str, str] = {}

        async def one(sid: str, sess: ServerSession) -> None:
            try:
                await asyncio.wait_for(sess.ensure(), timeout)
                status[sid] = f"{len(sess.tools)} tools"
            except Exception as e:
                status[sid] = f"unavailable ({type(e).__name__})"
                log.error("mcp server %s unavailable: %s", sid, sess.last_error or e)
        await asyncio.gather(*(one(k, v) for k, v in self.sessions.items()))
        return status

    async def stop(self) -> None:
        await asyncio.gather(*(s.reset() for s in self.sessions.values()), return_exceptions=True)

    def catalog(self) -> dict[str, ToolInfo]:
        out: dict[str, ToolInfo] = {}
        for s in self.sessions.values():
            out.update(s.tools)
        return out

    def tool(self, name: str) -> ToolInfo:
        t = self.catalog().get(name)
        if t is None:
            server = name.split(".", 1)[0]
            if server in self.sessions and not self.sessions[server].tools:
                raise UARError("unavailable", f"MCP server {server} is not connected")
            raise UARError("not_found", f"tool {name} not found")
        return t

    def visible_tools(self, p: Principal) -> list[ToolInfo]:
        if not p.has("tools:list"):
            raise UARError("permission_denied", "missing permission tools:list")
        return [t for t in self.catalog().values() if tool_visible(self.s.tool_policies, p, t.name)]

    def authorize(self, p: Principal, t: ToolInfo, args: dict, ctx: CallContext) -> str:
        """Raise unless this exact call is allowed. Returns the policy reason."""
        p.require("tools:execute")
        if ctx.agent_tools is not None and not matches_any(t.name, ctx.agent_tools):
            POLICY_DENIALS.labels("agent_tool").inc()
            raise UARError("policy_denied", f"agent is not permitted to use {t.name}")
        ok, reason = tool_decision(self.s.tool_policies, p, t.name, args)
        if not ok:
            POLICY_DENIALS.labels("tool").inc()
            raise UARError("policy_denied", reason, details={"tool": t.name})
        try:
            jsonschema.validate(args, t.input_schema)
        except jsonschema.ValidationError as e:
            raise UARError("invalid_argument", f"invalid arguments for {t.name}: {e.message}",
                           details={"path": list(e.absolute_path)}) from None
        return reason

    async def execute(self, p: Principal, name: str, args: dict, ctx: CallContext) -> ToolOutcome:
        t = self.tool(name)
        try:
            reason = self.authorize(p, t, args, ctx)
        except UARError as e:
            await self.audit.record(p, "tool.execute", name, "denied", request_id=ctx.request_id, run_id=ctx.run_id,
                                    details={"reason": e.message, "args_keys": sorted(args)}, required=False)
            TOOL_CALLS.labels(name, "denied").inc()
            raise
        ah = args_hash(name, args)
        await self.audit.record(p, "tool.execute", name, "intent", request_id=ctx.request_id, run_id=ctx.run_id,
                                details={"args_hash": ah, "side_effect": t.side_effect, "policy": reason})
        intent_id = None
        if t.side_effect != "read":
            # Durable intent before dispatch (the engine may pre-assign the id in its checkpoint).
            intent_id = ctx.intent_id or "ti_" + secrets.token_hex(10)
            await self.record_intent(p, intent_id, t, ah, ctx)
        t0 = time.monotonic()
        with get_tracer().start_as_current_span("tool.execute") as span:
            span.set_attributes({"uar.tool": name, "uar.tool.side_effect": t.side_effect})
            try:
                result = await self._call(t, args)
            except UARError as e:
                TOOL_CALLS.labels(name, "error").inc()
                await self._finish(p, intent_id, t, ctx, "failed" if e.code != "deadline_exceeded" else None,
                                   {"error": e.code})
                raise
            outcome = self._normalise(t, result, int((time.monotonic() - t0) * 1000))
            outcome.intent_id = intent_id
            span.set_attribute("uar.tool.is_error", outcome.is_error)
        TOOL_SECONDS.labels(name).observe(time.monotonic() - t0)
        TOOL_CALLS.labels(name, "error" if outcome.is_error else "ok").inc()
        await self._finish(p, intent_id, t, ctx, "failed" if outcome.is_error else "completed",
                           {"is_error": outcome.is_error, "truncated": outcome.truncated}, outcome.to_dict())
        return outcome

    async def record_intent(self, p: Principal, intent_id: str, t: ToolInfo, ah: str, ctx: CallContext) -> None:
        try:
            await self.store.execute(
                "INSERT INTO tool_intents (intent_id, tenant, run_id, node_id, tool, side_effect, args_hash, status)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,'intent') ON CONFLICT (intent_id) DO NOTHING",
                intent_id, p.tenant, ctx.run_id, ctx.node_id, t.name, t.side_effect, ah)
        except Exception as e:
            raise UARError("audit_unavailable", "cannot record tool intent; write not performed") from e

    async def _finish(self, p: Principal, intent_id: str | None, t: ToolInfo, ctx: CallContext, status: str | None,
                      details: dict, result: dict | None = None) -> None:
        # status None = outcome unknown (timeout on a write): leave the intent open -> ambiguous.
        # The intent row keeps the full outcome so a resumed run can reuse it instead of repeating
        # the write; the audit row keeps only a summary (no tool content).
        if intent_id and status:
            try:
                await self.store.execute("UPDATE tool_intents SET status=%s, completed_at=now(), result=%s "
                                         "WHERE intent_id=%s AND tenant=%s", status, jsonb(result or details),
                                         intent_id, p.tenant)
            except Exception as e:
                log.error("tool intent completion not recorded: %s", type(e).__name__)
        await self.audit.record(p, "tool.execute", t.name, status or "unknown", request_id=ctx.request_id,
                                run_id=ctx.run_id, details=details, required=False)

    async def _call(self, t: ToolInfo, args: dict):
        sess = self.sessions[t.server]
        try:
            return await sess.call(t.remote_name, args, t.timeout_s)
        except asyncio.TimeoutError:
            raise UARError("deadline_exceeded", f"tool {t.name} timed out after {t.timeout_s}s") from None
        except UARError:
            raise
        except Exception as e:
            # Transport failure: reconnect. Only read-only tools are retried; a write may have happened.
            log.warning("mcp call failed on %s: %s", t.server, type(e).__name__)
            await sess.reset()
            if t.side_effect == "read":
                try:
                    return await sess.call(t.remote_name, args, t.timeout_s)
                except asyncio.TimeoutError:
                    raise UARError("deadline_exceeded", f"tool {t.name} timed out") from None
                except UARError:
                    raise
                except Exception as e2:
                    await sess.reset()
                    raise UARError("unavailable", f"MCP server {t.server} failed",
                                   details={"error": type(e2).__name__}) from e2
            raise UARError("unavailable", f"MCP server {t.server} failed during a {t.side_effect} call; "
                           "the outcome is unknown", retryable=False, details={"error": type(e).__name__}) from e

    def _normalise(self, t: ToolInfo, result: Any, ms: int) -> ToolOutcome:
        cap = self.s.mcp_servers[[c.id for c in self.s.mcp_servers].index(t.server)].max_output_bytes
        blocks, used, truncated = [], 0, False
        for c in result.content or []:
            if getattr(c, "type", "") == "text":
                txt = c.text
                if used + len(txt) > cap:
                    txt = txt[: max(0, cap - used)]
                    truncated = True
                used += len(txt)
                blocks.append({"type": "text", "text": txt})
            else:
                blocks.append({"type": "text", "text": f"[{getattr(c, 'type', 'content')} omitted]"})
        structured = result.structured_content
        if structured is not None and len(dumps(structured)) > cap:
            structured, truncated = None, True
        if isinstance(structured, dict) and set(structured) == {"result"}:
            structured = structured["result"]
        return ToolOutcome(t.name, bool(result.is_error), blocks, structured, truncated, ms, side_effect=t.side_effect)
