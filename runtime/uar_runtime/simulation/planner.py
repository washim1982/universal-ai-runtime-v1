"""Dry-run: static planning and deterministic fixture simulation.

This package never executes anything. It must not import provider adapters, the MCP client,
the executor, or any network/process library; tests/test_dryrun.py enforces this with an
import-graph check and by making every execution entry point fail if called.

Inputs are plain data snapshots (tool catalog, route context, a graph loader that reads the
database). Unknown information is reported as unresolved, never invented.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from decimal import Decimal
from typing import Any, Awaitable, Callable

import jsonschema

from ..config import Settings, TenantCfg
from ..engine import cel
from ..engine.graph import Graph
from ..errors import UARError
from ..governance import Principal, tool_decision, tool_effect, tool_rule
from ..router.resolve import RouteContext, check_route_policy, price_for, resolve

MAX_BRANCHES = 32
EST_CHARS_PER_TOKEN = 4


@dataclass
class SimInputs:
    settings: Settings
    principal: Principal
    tenant: TenantCfg
    route_ctx: RouteContext
    tools: dict[str, dict]                                  # name -> {side_effect, input_schema}
    load_graph: Callable[[str, str], Awaitable[Graph]]


@dataclass
class Report:
    mode: str
    valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    routes: list[dict] = field(default_factory=list)
    permissions_required: set[str] = field(default_factory=set)
    permissions_missing: set[str] = field(default_factory=set)
    unresolved: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost: Decimal | None = Decimal(0)

    def need(self, p: Principal, perm: str) -> None:
        self.permissions_required.add(perm)
        if not p.has(perm):
            self.permissions_missing.add(perm)

    def to_dict(self, currency: str) -> dict:
        d = {"mode": self.mode, "valid": self.valid and not self.errors, "errors": self.errors,
             "warnings": self.warnings, "steps": self.steps, "routes": self.routes,
             "permissions_required": sorted(self.permissions_required),
             "permissions_missing": sorted(self.permissions_missing), "unresolved": self.unresolved,
             "branches": self.branches, "estimated_input_tokens_max": self.tokens_in,
             "estimated_output_tokens_max": self.tokens_out, "executed_nothing": True}
        if self.cost is not None:
            d["estimated_cost_max"] = {"amount": f"{self.cost:.8f}".rstrip("0").rstrip(".") or "0",
                                       "currency": currency}
        return d


def _est(text: str) -> int:
    return (len(text) + EST_CHARS_PER_TOKEN - 1) // EST_CHARS_PER_TOKEN


def _loop_multipliers(g: Graph) -> dict[str, int]:
    """Worst-case visits per node: product of max_iterations of enclosing loops."""
    mult = {n: 1 for n in g.nodes}
    for lid, ln in g.nodes.items():
        if ln["type"] != "loop":
            continue
        body, stack = set(), [ln["body"]]
        while stack:
            cur = stack.pop()
            if cur == lid or cur in body:
                continue
            body.add(cur)
            stack += [e["to"] for e in g.edges[cur]]
            if g.nodes[cur]["type"] == "loop":
                stack.append(g.nodes[cur]["body"])
        for n in body:
            mult[n] *= ln["max_iterations"]
    return mult


def _paths(g: Graph) -> list[str]:
    """Enumerate start-to-return paths. A loop is entered once (plus a zero-iteration path when it has
    a `while` guard); returning to it takes its exit edges. Capped at MAX_BRANCHES."""
    out: list[str] = []

    def walk(n: str, path: list[str]) -> None:
        if len(out) >= MAX_BRANCHES:
            return
        node = g.nodes[n]
        if node["type"] == "loop":
            if n in path:
                for e in g.edges[n]:
                    walk(e["to"], path + [f"{n}(exit)"])
                return
            walk(node["body"], path + [n])
            if node.get("while"):
                for e in g.edges[n]:
                    walk(e["to"], path + [n, f"{n}(exit)"])
            return
        if n in path:
            return
        path = path + [n]
        if node["type"] == "return":
            out.append(" -> ".join(path))
            return
        for e in g.edges[n]:
            walk(e["to"], path)

    walk(g.start, [])
    return out


async def static_plan(inp: SimInputs, g: Graph, report: Report | None = None, prefix: str = "",
                      depth: int = 0) -> Report:
    r = report or Report("static")
    p, s = inp.principal, inp.settings
    r.warnings += [prefix + w for w in g.warnings]
    mult = _loop_multipliers(g)
    if not prefix:
        r.branches = _paths(g)
        r.need(p, "runs:start")
    for nid, node in g.nodes.items():
        subs = [(nid, node)] + [(f"{nid}.{b}", bn) for b, bn in node.get("branches", {}).items()]
        for sid, sn in subs:
            step = await _plan_node(inp, r, g, prefix + sid, sn, mult[nid], depth)
            if step:
                r.steps.append(step)
    return r


async def _plan_node(inp: SimInputs, r: Report, g: Graph, sid: str, n: dict, visits: int, depth: int) -> dict | None:
    t, p, s = n["type"], inp.principal, inp.settings
    step = {"node_id": sid, "node_type": t, "detail": "", "permissions": [], "status": "planned"}
    if t == "llm":
        rctx = RouteContext(p, inp.tenant, inp.route_ctx.data_class, ("json",) if n.get("output_schema") else (),
                            inp.route_ctx.catalog, inp.route_ctx.unavailable)
        try:
            route = resolve(s, n["model"], rctx, check_policy=False)
        except UARError as e:
            step.update(status="unresolved", detail=f"{n['model']}: {e.message}")
            r.unresolved.append(f"{sid}: {e.message}")
            r.cost = None
            return step
        perm = f"inference:{route.model_class}"
        r.need(p, perm)
        step["permissions"] = [perm]
        step["detail"] = route.qualified
        try:
            check_route_policy(s, route, rctx)
        except UARError as e:
            step["status"] = "denied"
            if e.code != "permission_denied":  # missing permissions are listed; policy denials are errors
                r.errors.append(f"{sid}: {e.message}")
        r.routes.append(route.to_dict())
        if inp.route_ctx.catalog.get(route.provider) is None:
            r.unresolved.append(f"{sid}: availability of {route.qualified} unknown (catalog not discovered)")
        params = n.get("params") or {}
        tin = _est(n.get("system", "") + n["prompt"]) + (_est(str(n.get("output_schema"))) if n.get("output_schema")
                                                          else 0)
        tout = params.get("max_tokens") or s.router.default_max_tokens
        r.tokens_in += tin * visits
        r.tokens_out += tout * visits
        if cel.expressions(n["prompt"], template=True):
            r.warnings.append(f"{sid}: prompt size depends on runtime values; input estimate covers the template only")
        price = price_for(s, route.provider, route.model, tin * visits, tout * visits)
        if price is None:
            if r.cost is not None:
                r.unresolved.append(f"{sid}: no price configured for {route.provider}/{route.model}; cost unknown")
            r.cost = None
        elif r.cost is not None:
            r.cost += price
    elif t == "tool":
        r.need(p, "tools:execute")
        step["permissions"] = ["tools:execute"]
        info = inp.tools.get(n["tool"])
        if info is None:
            step["status"] = "unresolved"
            step["detail"] = f"{n['tool']}: not in the cached tool catalog"
            r.unresolved.append(step["detail"])
            return step
        step["detail"] = f"{n['tool']} ({info['side_effect']})"
        args = n.get("args") or {}
        constrained = [pol for pol in s.tool_policies if pol.args and fnmatchcase(n["tool"], pol.tool)]
        if cel.expressions(args):
            if constrained:
                step["status"] = "unresolved"
                r.unresolved.append(f"{sid}: tool policy argument checks depend on runtime values")
        else:
            ok, why = tool_decision(s.tool_policies, p, n["tool"], args)
            if not ok:
                step["status"] = "denied"
                r.errors.append(f"{sid}: {why}")
            try:
                jsonschema.validate(args, info.get("input_schema") or {})
            except jsonschema.ValidationError as e:
                r.errors.append(f"{sid}: invalid arguments: {e.message}")
        if step["status"] == "planned" and cel.expressions(args) and not constrained:
            ok, why = tool_decision(s.tool_policies, p, n["tool"], {})
            if not ok:
                step["status"] = "denied"
                r.errors.append(f"{sid}: {why}")
        if info["side_effect"] != "read":
            r.warnings.append(f"{sid}: {info['side_effect']} tool - a real run records a durable intent before "
                              "the call and never retries it automatically")
        rule = tool_rule(s.tool_policies, p, n["tool"])
        needs = (rule is not None and rule.effect == "require_approval") if cel.expressions(args) else             tool_effect(s.tool_policies, p, n["tool"], args)[0] == "require_approval"
        if step["status"] != "denied" and needs:
            step["detail"] += " - needs approval"
            r.warnings.append(f"{sid}: {n['tool']} requires a human approval of its exact arguments; a real run "
                              "pauses (waiting_approval) until it is decided")
    elif t == "agent":
        if depth + 1 > g.limits["max_depth"]:
            r.errors.append(f"{sid}: exceeds max_depth {g.limits['max_depth']}")
            return step
        try:
            child = await inp.load_graph(n["agent"], n.get("version", ""))
        except UARError as e:
            step["status"] = "unresolved"
            r.unresolved.append(f"{sid}: {e.message}")
            return step
        step["detail"] = f"sub-agent {child.agent_id}@{child.version}"
        await static_plan(inp, child, r, prefix=f"{sid}/", depth=depth + 1)
    elif t == "approval":
        roles = n.get("approvers") or []
        step["detail"] = f"approval of {n['action']}" + (f" by {', '.join(roles)}" if roles else "")
        r.warnings.append(f"{sid}: pauses the run until a human decides; a rejection or expiry "
                          + ("continues with approved=false" if n.get("on_reject") == "continue" else "fails the run"))
    elif t in ("transform", "condition", "loop", "return", "parallel"):
        if t == "loop":
            step["detail"] = f"up to {n['max_iterations']} iterations"
    return step


async def simulate(inp: SimInputs, g: Graph, input_: dict, fixtures: dict, seed: int) -> Report:
    """Follow one path using fixture outputs for llm/tool/agent nodes. Pure evaluation otherwise."""
    r = await static_plan(inp, g)
    r.mode = "simulate"
    static_steps, r.steps = {st["node_id"]: st for st in r.steps}, []
    r.branches = []
    visits: dict[str, int] = {}
    st = {"nodes": {}, "memory": {}, "loops": {}}
    nid = g.start
    path = []
    steps = 0

    def ctx() -> dict:
        return {"input": input_, "nodes": st["nodes"], "memory": st["memory"],
                "loop": {k: {"iteration": v} for k, v in st["loops"].items()}, "run": {"id": "dry-run",
                                                                                       "agent_id": g.agent_id}}

    def fixture(node_id: str):
        f = fixtures.get(node_id)
        if f is None:
            return None, False
        k = visits.get(node_id, 0)
        if isinstance(f, list):
            if k >= len(f):
                return None, False
            return f[k], True
        return f, True

    try:
        while nid is not None:
            steps += 1
            if steps > g.limits["max_steps"]:
                r.errors.append(f"simulation hit max_steps {g.limits['max_steps']}")
                break
            node = g.nodes[nid]
            path.append(nid)
            step = dict(static_steps.get(nid, {"node_id": nid, "node_type": node["type"], "permissions": []}))
            t = node["type"]
            if t == "tool":
                # Arguments are concrete here, so argument-dependent policy checks can be decided.
                args = cel.render_mapping(node.get("args") or {}, ctx())
                ok_policy, why = tool_decision(inp.settings.tool_policies, inp.principal, node["tool"], args)
                r.unresolved = [u for u in r.unresolved
                                if u != f"{nid}: tool policy argument checks depend on runtime values"]
                if not ok_policy:
                    step["status"] = "denied"
                    r.steps.append(step)
                    r.errors.append(f"{nid}: {why}")
                    break
            if t in ("llm", "tool", "agent", "parallel", "approval"):
                out, ok = fixture(nid)
                if not ok:
                    step["status"] = "unresolved"
                    r.steps.append(step)
                    r.unresolved.append(f"{nid}: no fixture for visit {visits.get(nid, 0) + 1}; simulation stops here")
                    break
                if t == "llm" and node.get("output_schema"):
                    try:
                        jsonschema.validate(out, node["output_schema"])
                    except jsonschema.ValidationError as e:
                        r.errors.append(f"{nid}: fixture does not match output_schema: {e.message}")
                        break
                if t == "llm" and not node.get("output_schema") and not isinstance(out, dict):
                    out = {"text": str(out)}
                if t == "approval":
                    out = out if isinstance(out, dict) else {"approved": bool(out)}
                    out.setdefault("status", "approved" if out.get("approved") else "rejected")
                    if not out.get("approved") and node.get("on_reject", "fail") != "continue":
                        step.update({"status": "simulated", "output": out})
                        r.steps.append(_structify(step))
                        r.errors.append(f"{nid}: {node['action']} not approved; the run fails here")
                        break
                visits[nid] = visits.get(nid, 0) + 1
                step.update({"status": "simulated", "output": out})
                st["nodes"][nid] = {"output": out}
                nid = _next(g, nid, ctx())
            elif t == "loop":
                count = st["loops"].get(nid, 0)
                cond = True if not node.get("while") else cel.evaluate(node["while"].strip()[2:-1], ctx()) is True
                if cond and count < node["max_iterations"]:
                    st["loops"][nid] = count + 1
                    step.update({"status": "simulated", "output": {"iteration": count + 1}})
                    nid = node["body"]
                else:
                    st["loops"][nid] = 0
                    st["nodes"][nid] = {"output": {"iteration": count, "exhausted": cond}}
                    step.update({"status": "simulated", "output": st["nodes"][nid]["output"]})
                    nid = _next(g, nid, ctx())
            elif t == "condition":
                step["status"] = "simulated"
                nid = _next(g, nid, ctx())
            elif t == "transform":
                for k, v in (node.get("set") or {}).items():
                    st["memory"][k] = cel.render_mapping(v, ctx())
                out = cel.render_mapping(node.get("value"), ctx())
                st["nodes"][nid] = {"output": out}
                step.update({"status": "simulated", "output": out})
                nid = _next(g, nid, ctx())
            elif t == "return":
                out = cel.render_mapping(node.get("value"), ctx())
                step.update({"status": "simulated", "output": out if isinstance(out, dict) else {"value": out}})
                nid = None
            r.steps.append(_structify(step))
    except UARError as e:
        r.errors.append(f"{path[-1] if path else g.start}: {e.message}")
    r.branches = [" -> ".join(path)]
    return r


def _structify(step: dict) -> dict:
    if "output" in step and not isinstance(step["output"], dict):
        step["output"] = {"value": step["output"]}
    return step


def _next(g: Graph, nid: str, ctx: dict) -> str:
    for e in g.edges[nid]:
        if "when" not in e or cel.evaluate(e["when"].strip()[2:-1], ctx) is True:
            return e["to"]
    raise UARError("failed_precondition", f"{nid}: no outgoing edge matched")


def preview_inference(inp: SimInputs, model: str, caps: tuple[str, ...], data_class: str) -> Report:
    r = Report("static")
    try:
        route = resolve(inp.settings, model, RouteContext(inp.principal, inp.tenant, data_class, caps,
                                                          inp.route_ctx.catalog, inp.route_ctx.unavailable))
        r.routes.append(route.to_dict())
        r.need(inp.principal, f"inference:{route.model_class}")
        if inp.route_ctx.catalog.get(route.provider) is None:
            r.unresolved.append(f"availability of {route.qualified} unknown (catalog not discovered)")
        if price_for(inp.settings, route.provider, route.model, 0, 0) is None:
            r.cost = None
            r.unresolved.append(f"no price configured for {route.provider}/{route.model}")
    except UARError as e:
        r.errors.append(e.message)
        r.cost = None
    return r
