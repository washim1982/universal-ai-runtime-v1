"""Shared conformance fixtures replayed against the live runtime (the SDKs replay the same files)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest

from conftest import headers

FIXTURES = sorted((Path(__file__).resolve().parents[2] / "contracts" / "fixtures").glob("*.json"))


def strip(value, ignore: list[str]):
    """Remove dotted paths from a copy of value (applied to each event for streams)."""
    v = copy.deepcopy(value)
    for path in ignore:
        cur, parts = v, path.split(".")
        for p in parts[:-1]:
            cur = cur.get(p, {}) if isinstance(cur, dict) else {}
        if isinstance(cur, dict):
            cur.pop(parts[-1], None)
    return v


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
async def test_fixture_matches_runtime(env, path):
    fx = json.loads(path.read_text(encoding="utf-8"))
    req, exp = fx["request"], fx["response"]
    key = {"dev": env.keys.dev, "none": None}[fx["auth"]]
    async with httpx.AsyncClient(base_url=env.http, headers=headers(key) if key else {}) as c:
        if exp.get("events") is not None:
            async with c.stream(req["method"], req["path"], json=req.get("body")) as r:
                assert r.status_code == exp["status"]
                got = [json.loads(line[5:]) async for line in r.aiter_lines() if line.startswith("data:")]
            got = [{k: v for k, v in e.items() if k != "type"} for e in got]
            assert [strip(e, exp["ignore"]) for e in got] == [strip(e, exp["ignore"]) for e in exp["events"]]
        else:
            r = await c.request(req["method"], req["path"], json=req.get("body"))
            assert r.status_code == exp["status"], r.text
            assert strip(r.json(), exp["ignore"]) == strip(exp["body"], exp["ignore"])
