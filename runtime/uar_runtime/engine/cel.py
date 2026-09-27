"""Sandboxed expressions for agent graphs.

- `${ expr }` inside mapping values is CEL (no I/O, no arbitrary code). A string that is exactly one
  `${ ... }` keeps the expression's type; otherwise each `${ ... }` is interpolated as text.
- `{{ expr }}` inside prompt templates is CEL rendered as text (JSON for maps and lists).
Context variables: input, nodes.<id>.output, memory, loop.<id>.iteration, run.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, Iterator

import celpy

from ..errors import UARError

_env = celpy.Environment()
_NODE_REF = re.compile(r"\bnodes\s*\.\s*([a-z][a-z0-9_]*)")


@lru_cache(maxsize=2048)
def _program(src: str):
    try:
        return _env.program(_env.compile(src))
    except Exception as e:  # celpy raises CELParseError and friends
        raise UARError("invalid_graph", f"invalid expression: {src!r}", details={"error": str(e)[:200]}) from None


def check(src: str) -> None:
    _program(src.strip())


def evaluate(src: str, ctx: dict) -> Any:
    prog = _program(src.strip())
    try:
        result = prog.evaluate({k: celpy.json_to_cel(v) for k, v in ctx.items()})
    except Exception as e:
        raise UARError("invalid_argument", f"expression failed: {src.strip()!r}",
                       details={"error": str(e)[:200]}) from None
    return json.loads(json.dumps(result, cls=celpy.CELJSONEncoder))


def _scan(text: str, opener: str) -> Iterator[tuple[int, int, str]]:
    """Yield (start, end, expr) for each ${...} (brace-balanced, quote-aware) or {{...}}."""
    i = 0
    while True:
        s = text.find(opener, i)
        if s == -1:
            return
        if opener == "{{":
            e = text.find("}}", s + 2)
            if e == -1:
                raise UARError("invalid_graph", "unterminated {{ in template")
            yield s, e + 2, text[s + 2:e]
            i = e + 2
            continue
        depth, j, quote = 1, s + 2, None
        while j < len(text) and depth:
            ch = text[j]
            if quote:
                if ch == "\\":
                    j += 1
                elif ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            j += 1
        if depth:
            raise UARError("invalid_graph", "unterminated ${ in expression")
        yield s, j, text[s + 2:j - 1]
        i = j


def _text(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v)


def render_mapping(value: Any, ctx: dict) -> Any:
    if isinstance(value, dict):
        return {k: render_mapping(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render_mapping(v, ctx) for v in value]
    if isinstance(value, str) and "${" in value:
        parts = list(_scan(value, "${"))
        if len(parts) == 1 and parts[0][0] == 0 and parts[0][1] == len(value):
            return evaluate(parts[0][2], ctx)
        out, last = [], 0
        for s, e, expr in parts:
            out.append(value[last:s])
            out.append(_text(evaluate(expr, ctx)))
            last = e
        out.append(value[last:])
        return "".join(out)
    return value


def render_template(text: str, ctx: dict) -> str:
    out, last = [], 0
    for s, e, expr in _scan(text, "{{"):
        out.append(text[last:s])
        out.append(_text(evaluate(expr, ctx)))
        last = e
    out.append(text[last:])
    return "".join(out)


def expressions(value: Any, template: bool = False) -> list[str]:
    """All expressions inside a mapping (or a template when template=True)."""
    if isinstance(value, dict):
        return [x for v in value.values() for x in expressions(v, template)]
    if isinstance(value, list):
        return [x for v in value for x in expressions(v, template)]
    if isinstance(value, str):
        return [e for _, _, e in _scan(value, "{{" if template else "${")]
    return []


def node_refs(expr: str) -> set[str]:
    return set(_NODE_REF.findall(expr))
