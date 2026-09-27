"""Agent graph compiler. Pure: validates a definition and produces an immutable Graph.

Checks: JSON Schema; unique ids; start and edge targets exist; every non-return node has a way
out; return nodes are terminal; every node reachable from start; every cycle passes through a
loop node; expressions parse and reference existing nodes; declared permissions cover every
model/tool/agent used; limits within administrator bounds.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

from ..config import LimitsCfg
from ..errors import UARError
from ..governance import matches_any
from . import cel

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "contracts" / "schemas" / "agent.schema.json"


@lru_cache(maxsize=1)
def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def canonical(definition: dict) -> str:
    return json.dumps(definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(definition: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical(definition).encode()).hexdigest()


@dataclass
class Graph:
    agent_id: str
    version: str
    digest: str
    start: str
    nodes: dict[str, dict]
    edges: dict[str, list[dict]]          # from -> ordered edges
    limits: dict[str, Any]
    permissions: dict[str, list[str]]
    input_schema: dict | None
    output_schema: dict | None
    definition: dict
    warnings: list[str] = field(default_factory=list)


def compile_graph(definition: dict, limits: LimitsCfg) -> Graph:
    errors = sorted(_validator().iter_errors(definition), key=lambda e: list(e.absolute_path))
    if errors:
        e = errors[0]
        where = "/".join(str(p) for p in e.absolute_path) or "(root)"
        raise UARError("invalid_graph", f"{where}: {e.message}", details={"errors": len(errors)})
    meta, spec = definition["metadata"], definition["spec"]
    nodes: dict[str, dict] = {}
    for n in spec["nodes"]:
        if n["id"] in nodes:
            raise UARError("invalid_graph", f"duplicate node id {n['id']}")
        nodes[n["id"]] = n
    problems: list[str] = []
    warnings: list[str] = []
    if spec["start"] not in nodes:
        problems.append(f"start node {spec['start']} does not exist")
    edges: dict[str, list[dict]] = {nid: [] for nid in nodes}
    for e in spec.get("edges", []):
        for end in ("from", "to"):
            if e[end] not in nodes:
                problems.append(f"edge {e['from']}->{e['to']}: unknown node {e[end]}")
        if e["from"] in edges:
            edges[e["from"]].append(e)
    for nid, n in nodes.items():
        t, out = n["type"], edges[nid]
        if t == "approval":
            problems.append(f"{nid}: approval nodes are not available until M9")
        if t == "return" and out:
            problems.append(f"{nid}: return nodes cannot have outgoing edges")
        if t != "return" and not out:
            problems.append(f"{nid}: no outgoing edge (only return nodes may end a run)")
        if t == "loop" and n["body"] not in nodes:
            problems.append(f"{nid}: loop body {n['body']} does not exist")
        if t == "condition" and out and all("when" in e for e in out):
            warnings.append(f"{nid}: every edge has a guard; the run fails if none matches")
    if problems:
        raise UARError("invalid_graph", problems[0], details={"problems": problems})

    succ = {nid: [e["to"] for e in edges[nid]] + ([nodes[nid]["body"]] if nodes[nid]["type"] == "loop" else [])
            for nid in nodes}
    seen, stack = set(), [spec["start"]]
    while stack:
        cur = stack.pop()
        if cur not in seen:
            seen.add(cur)
            stack.extend(succ[cur])
    unreachable = sorted(set(nodes) - seen)
    if unreachable:
        raise UARError("invalid_graph", f"unreachable nodes: {', '.join(unreachable)}")
    _check_cycles(nodes, succ)

    lim = _limits(spec.get("limits", {}), limits)
    perms = {k: list(v) for k, v in (spec.get("permissions") or {}).items()}
    for nid, n in nodes.items():
        for sub_id, sub in ([(nid, n)] + [(f"{nid}.{b}", bn) for b, bn in n.get("branches", {}).items()]):
            _check_node_exprs(sub_id, sub, nodes)
            _check_node_perms(sub_id, sub, perms)
        if n["type"] == "loop" and n["max_iterations"] > lim["max_loop_iterations"]:
            raise UARError("invalid_graph", f"{nid}: max_iterations exceeds limit {lim['max_loop_iterations']}")
    for e in spec.get("edges", []):
        if "when" in e:
            _check_expr(f"edge {e['from']}->{e['to']}", e["when"][2:-1], nodes)
    return Graph(meta["id"], meta["version"], digest(definition), spec["start"], nodes, edges, lim, perms,
                 spec.get("input"), spec.get("output"), definition, warnings)


def _check_cycles(nodes: dict[str, dict], succ: dict[str, list[str]]) -> None:
    """Every cycle must pass through a loop node: the graph without loop nodes must be acyclic."""
    plain = {n: [m for m in succ[n] if nodes[m]["type"] != "loop"] for n in nodes if nodes[n]["type"] != "loop"}
    state: dict[str, int] = {}

    def visit(n: str, path: list[str]) -> None:
        state[n] = 1
        for m in plain[n]:
            if state.get(m) == 1:
                cyc = path[path.index(m):] + [m] if m in path else [n, m]
                raise UARError("invalid_graph", "cycle without a loop node: " + " -> ".join(cyc))
            if m not in state:
                visit(m, path + [m])
        state[n] = 2

    for n in plain:
        if n not in state:
            visit(n, [n])


def _limits(declared: dict, admin: LimitsCfg) -> dict[str, Any]:
    bounds = admin.model_dump()
    out = dict(bounds)
    for k, v in declared.items():
        if k == "max_cost_usd":
            out[k] = v
            continue
        if k in bounds and v > bounds[k]:
            raise UARError("invalid_graph", f"limits.{k}={v} exceeds the administrator bound {bounds[k]}")
        out[k] = v
    out.setdefault("max_cost_usd", None)
    return out


def _check_expr(where: str, expr: str, nodes: dict) -> None:
    try:
        cel.check(expr)
    except UARError as e:
        raise UARError("invalid_graph", f"{where}: {e.message}", details=e.details) from None
    missing = cel.node_refs(expr) - set(nodes)
    if missing:
        raise UARError("invalid_graph", f"{where}: references unknown node(s) {sorted(missing)}")


def _check_node_exprs(nid: str, n: dict, nodes: dict) -> None:
    for key in ("args", "value", "set", "input"):
        for x in cel.expressions(n.get(key)):
            _check_expr(f"{nid}.{key}", x, nodes)
    for key in ("prompt", "system"):
        for x in cel.expressions(n.get(key), template=True):
            _check_expr(f"{nid}.{key}", x, nodes)
    if n.get("while"):
        _check_expr(f"{nid}.while", n["while"].strip()[2:-1], nodes)


def _check_node_perms(nid: str, n: dict, perms: dict[str, list[str]]) -> None:
    t = n["type"]
    if t == "llm" and not matches_any(n["model"], perms.get("models", [])):
        raise UARError("invalid_graph", f"{nid}: model {n['model']} is not in permissions.models")
    if t == "tool" and not matches_any(n["tool"], perms.get("tools", [])):
        raise UARError("invalid_graph", f"{nid}: tool {n['tool']} is not in permissions.tools")
    if t == "agent" and not matches_any(n["agent"], perms.get("agents", [])):
        raise UARError("invalid_graph", f"{nid}: agent {n['agent']} is not in permissions.agents")
