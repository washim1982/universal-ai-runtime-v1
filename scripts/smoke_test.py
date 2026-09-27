"""End-to-end smoke test against a RUNNING runtime (local process, Docker Compose or Kubernetes).

    python scripts/smoke_test.py                                   # http://localhost:9000, keys from .local/credentials.env
    python scripts/smoke_test.py --http http://127.0.0.1:9000 --grpc 127.0.0.1:50051 --model local:default

Exercises every transport (HTTP, SSE, gRPC, WebSocket), governance, MCP tools, agents, dry-run and
metrics. Uses real models: needs the configured `local:default` model to be reachable.
Exit code 0 = all checks passed. Writes one small file under the workspace's reports/ folder.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import time
from pathlib import Path

import grpc
import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from uarpb.v1 import runtime_pb2 as pb  # noqa: E402
from uarpb.v1 import runtime_pb2_grpc as pbg  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


def keys_from_file() -> dict[str, str]:
    p = ROOT / ".local" / "credentials.env"
    if not p.exists():
        return {}
    return dict(line.split("=", 1) for line in p.read_text(encoding="utf-8").splitlines() if "=" in line)


def run(a: argparse.Namespace) -> None:
    creds = keys_from_file()
    dev, ops = a.dev_key or creds.get("UAR_DEV_KEY"), a.ops_key or creds.get("UAR_OPS_KEY")
    if not dev:
        sys.exit("no developer key: pass --dev-key or run scripts/bootstrap_local.py")
    H = {"X-API-Key": dev}
    c = httpx.Client(base_url=a.http, timeout=300)

    print("\n[1] health")
    r = c.get("/readyz")
    check("readyz", r.status_code == 200 and r.json().get("ready") is True, r.text[:120])

    print("\n[2] authentication and authorization")
    check("no credentials -> 401", c.get("/api/v1/models").status_code == 401)
    check("bad key -> 401", c.get("/api/v1/models", headers={"X-API-Key": "uar_deadbeef_xxxxxxxxxxxxxxxxxxxx"}).status_code == 401)
    if ops:
        r = c.post("/api/v1/tool/execute", headers={"X-API-Key": ops},
                   json={"tool": "fs.write_text", "args": {"path": "reports/x.md", "content": "x"}})
        check("operator cannot write files -> 403", r.status_code == 403)
    r = c.post("/api/v1/inference", headers=H, json={"model": "cloud:default", "input": "x"})
    check("cloud model without permission -> 403", r.status_code == 403, r.json()["error"]["code"])
    r = c.post("/api/v1/inference", headers=H, json={"model": a.model, "input": "x", "bogus": 1})
    check("unknown request field -> 400", r.status_code == 400)

    print("\n[3] inference")
    r = c.post("/api/v1/inference", headers=H, json={"model": a.model, "input": "Reply with the word ready.",
                                                     "params": {"max_tokens": 20, "temperature": 0}})
    body = r.json()
    check("HTTP sync", r.status_code == 200 and bool(body.get("content")),
          f"{body.get('provider')}/{body.get('model')} {body.get('usage', {}).get('output_tokens')} tokens")
    with c.stream("POST", "/api/v1/inference", headers=H,
                  json={"model": a.model, "input": "Count from 1 to 3.", "stream": True, "params": {"max_tokens": 30}}) as s:
        events = [json.loads(line[5:]) for line in s.iter_lines() if line.startswith("data:")]
    kinds = [e["type"] for e in events]
    check("SSE stream: started ... usage ... completed", kinds[:1] == ["started"] and kinds[-1:] == ["completed"]
          and "usage" in kinds, f"{len(events)} events")
    with grpc.insecure_channel(a.grpc) as ch:
        g = pbg.RuntimeStub(ch).Infer(pb.InferenceRequest(model=a.model, input="Reply with the word ready."),
                                      metadata=[("x-api-key", dev)], timeout=300)
    check("gRPC Infer", bool(g.content), f"{g.provider}/{g.model}")
    ws_url = a.http.replace("http", "ws", 1) + "/api/v1/ws"

    async def ws_check() -> tuple[str, str]:
        async with websockets.connect(ws_url, additional_headers=H) as ws:
            await ws.send(json.dumps({"id": "1", "op": "infer", "body": {"model": a.model, "input": "Say hi",
                                                                         "params": {"max_tokens": 20}}}))
            text = ""
            while True:
                f = json.loads(await ws.recv())
                if f.get("event", {}).get("type") == "token":
                    text += f["event"]["token"]["text"]
                if f.get("done") or f.get("error"):
                    return text, "error" if f.get("error") else "done"
    try:
        text, end = asyncio.run(ws_check())
        check("WebSocket infer", end == "done" and bool(text))
    except Exception as e:
        check("WebSocket infer", False, type(e).__name__ + ": " + str(e)[:100])
    models = c.get("/api/v1/models", headers=H).json()["models"]
    check("model catalog", any(m["name"] == "local:default" for m in models), f"{len(models)} entries")

    print("\n[4] MCP tools")
    tools = {t["name"] for t in c.get("/api/v1/tools", headers=H).json()["tools"]}
    check("tool catalog", {"fs.read_text", "fs.write_text", "db.query"} <= tools, ", ".join(sorted(tools)))
    r = c.post("/api/v1/tool/execute", headers=H, json={"tool": "db.query", "args": {"sql": "SELECT COUNT(*) AS n FROM sales"}})
    check("read-only SQL query", r.status_code == 200 and not r.json()["is_error"], str(r.json().get("structured")))
    r = c.post("/api/v1/tool/execute", headers=H, json={"tool": "db.query", "args": {"sql": "DELETE FROM sales"}})
    check("SQL write rejected", r.json().get("is_error") is True)
    r = c.post("/api/v1/tool/execute", headers=H, json={"tool": "fs.read_text", "args": {"path": "../secret.txt"}})
    check("path traversal rejected", r.json().get("is_error") is True)
    r = c.post("/api/v1/tool/execute", headers=H, json={"tool": "fs.write_text", "args": {"path": "docs/x.md", "content": "x"}})
    check("write outside reports/ denied by policy", r.status_code == 403)
    name = f"reports/smoke-{secrets.token_hex(3)}.md"
    r = c.post("/api/v1/tool/execute", headers=H, json={"tool": "fs.write_text", "args": {"path": name, "content": "smoke"}})
    check("authorized write", r.status_code == 200 and not r.json()["is_error"], name)
    r = c.post("/api/v1/tool/execute", headers=H, json={"tool": "fs.write_text", "args": {"path": name, "content": "again"}})
    check("overwrite refused", r.json().get("is_error") is True)

    print("\n[5] agents")
    report = f"smoke_{secrets.token_hex(3)}"
    key = f"smoke-{secrets.token_hex(4)}"
    body = {"agent_id": "report_generator", "input": {"report_name": report}, "idempotency_key": key}
    r1 = c.post("/api/v1/agent/run", headers=H, json=body)
    r2 = c.post("/api/v1/agent/run", headers=H, json=body)
    check("start run -> 202; idempotent retry returns the same run",
          r1.status_code == 202 and r1.json()["run_id"] == r2.json()["run_id"])
    run_id = r1.json()["run_id"]
    deadline = time.time() + 600
    while True:
        run_ = c.get(f"/api/v1/runs/{run_id}", headers=H).json()
        if run_["status"] not in ("queued", "running") or time.time() > deadline:
            break
        time.sleep(1)
    check("report agent succeeded", run_["status"] == "succeeded", json.dumps(run_.get("output") or run_.get("error"))[:120])
    with c.stream("GET", f"/api/v1/runs/{run_id}/events", headers=H) as s:
        evs = [json.loads(line[5:]) for line in s.iter_lines() if line.startswith("data:")]
    done = [e["node_completed"]["node_id"] for e in evs if e["type"] == "node_completed"]
    check("run events replayed in order", done == ["sales", "analyse", "render", "write", "done"], " > ".join(done))
    r = c.get(f"/api/v1/runs/{run_id}", headers={"X-API-Key": "uar_deadbeef_xxxxxxxxxxxxxxxxxxxx"})
    check("run not readable without valid key", r.status_code == 401)
    r = c.post("/api/v1/agent/run", headers=H, json={"agent_id": "report_generator", "input": {"report_name": "../../x"}})
    check("agent input schema enforced", r.status_code == 400)
    r = c.post("/api/v1/inference", headers=H, json={"model": a.model, "agent": "in_app_assistant",
                                                     "input": "How many days do I have to return an item?"})
    check("inference with agent= (in-app assistant)", r.status_code == 200 and "30" in r.json().get("content", ""),
          r.json().get("content", "")[:80])

    print("\n[6] dry-run")
    r = c.post("/api/v1/dry-run", headers=H, json={"agent_id": "report_generator", "mode": "static"})
    d = r.json()
    check("static plan", d.get("valid") and d.get("executed_nothing"), " | ".join(d.get("branches", [])))
    fixtures = {"sales": {"columns": ["region"], "rows": [["North"]]}, "analyse": {"title": "T", "body": "- b"},
                "write": {"path": "reports/sim.md", "bytes": 1, "created": True}}
    d = c.post("/api/v1/dry-run", headers=H, json={"agent_id": "report_generator", "mode": "simulate",
                                                   "input": {"report_name": "sim"}, "fixtures": fixtures}).json()
    check("fixture simulation reaches return", d.get("valid") and d["steps"][-1]["status"] == "simulated")

    print("\n[7] observability")
    m = c.get("/metrics").text
    check("Prometheus metrics", "uar_model_tokens_total" in m and "uar_runs_total" in m)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", default="http://localhost:9000")
    ap.add_argument("--grpc", default="localhost:50051")
    ap.add_argument("--model", default="local:default")
    ap.add_argument("--dev-key")
    ap.add_argument("--ops-key")
    a = ap.parse_args()
    run(a)
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
