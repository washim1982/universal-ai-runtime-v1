"""M0 gate: the contracts are canonical, current, compatible and complete."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

from uar_runtime.config import LimitsCfg
from uar_runtime.engine.graph import compile_graph
from uar_runtime.errors import UARError

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable


def run(*args):
    return subprocess.run([PY, *args], cwd=ROOT, capture_output=True, text=True)


def test_generated_artifacts_are_current():
    r = run("scripts/gen_contracts.py", "--check")
    assert r.returncode == 0, r.stdout + r.stderr


def test_no_breaking_changes_against_baseline():
    r = run("scripts/check_breaking.py")
    assert r.returncode == 0, r.stdout


def test_breaking_change_detector_detects(tmp_path):
    from google.protobuf import descriptor_pb2
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_breaking
    base = check_breaking.index(ROOT / "contracts/baseline/descriptor.pb")
    fds = descriptor_pb2.FileDescriptorSet.FromString((ROOT / "contracts/baseline/descriptor.pb").read_bytes())
    msg = next(m for f in fds.file if f.package == "uar.v1" for m in f.message_type if m.name == "Usage")
    msg.field[0].number = 99
    p = tmp_path / "d.pb"
    p.write_bytes(fds.SerializeToString())
    problems = check_breaking.compare(base, check_breaking.index(p))
    assert any("uar.v1.Usage.input_tokens" in x for x in problems)


async def test_openapi_covers_every_http_route(env):
    from uar_runtime.gateway.http import create_app
    spec = json.loads((ROOT / "contracts/generated/openapi.json").read_text(encoding="utf-8"))
    served = {r.path for r in create_app(env.svc).routes
              if r.path.startswith("/api/v1/") and r.path not in ("/api/v1/openapi.json", "/api/v1/ws")}
    assert set(spec["paths"]) == served
    ops = {op["operationId"] for p in spec["paths"].values() for op in p.values()}
    assert "Infer" in ops and "InferStream" not in ops  # both share /inference; "stream" selects SSE


@pytest.mark.parametrize("path", sorted((ROOT / "examples/agents").glob("*.yaml")), ids=lambda p: p.stem)
def test_example_agents_compile(path):
    g = compile_graph(yaml.safe_load(path.read_text(encoding="utf-8")), LimitsCfg(timeout_s=600))
    assert g.agent_id == path.stem


def test_schema_rejects_invalid_examples():
    base = yaml.safe_load((ROOT / "examples/agents/in_app_assistant.yaml").read_text(encoding="utf-8"))
    for mutate in (lambda d: d.pop("kind"), lambda d: d["metadata"].update(version="v1"),
                   lambda d: d["spec"]["nodes"][0].update(extra=True),
                   lambda d: d["spec"]["nodes"][0].update(type="shell")):
        d = json.loads(json.dumps(base))
        mutate(d)
        with pytest.raises(UARError):
            compile_graph(d, LimitsCfg(timeout_s=600))


def test_conformance_fixtures_match_contract():
    schema = json.loads((ROOT / "contracts/generated/uar.v1.schema.json").read_text(encoding="utf-8"))
    fixtures = sorted((ROOT / "contracts/fixtures").glob("*.json"))
    assert fixtures, "no conformance fixtures"
    for f in fixtures:
        fx = json.loads(f.read_text(encoding="utf-8"))
        for part, msg in (("request", fx.get("request_type")), ("response", fx.get("response_type"))):
            if not msg or fx.get(part, {}).get("body") is None:
                continue
            v = jsonschema.Draft202012Validator({**schema, "$ref": f"#/$defs/{msg}"})
            errors = list(v.iter_errors(fx[part]["body"]))
            assert not errors, f"{f.name} {part}: {errors[0].message}"
        for ev in fx.get("response", {}).get("events", []):
            v = jsonschema.Draft202012Validator({**schema, "$ref": "#/$defs/Event"})
            assert not list(v.iter_errors(ev)), f"{f.name} event {ev}"
