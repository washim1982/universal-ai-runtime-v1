"""M9 gates: approvals (graph nodes and inline tool approvals), tamper-evident audit, OIDC/JWKS
identity, content redaction, retention, and cross-tenant isolation."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from conftest import headers, start_env, stop_env
from uar_runtime.errors import UARError
from uar_runtime.governance import Principal

ISSUER = "https://idp.example.test/"


# ------------------------------------------------------------------ identity provider (JWKS)

class Idp:
    """A local JWKS endpoint and token minting for OIDC tests."""

    def __init__(self):
        self.keys: dict[str, rsa.RSAPrivateKey] = {}
        self.published: list[str] = []
        idp = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"keys": [idp.jwk(k) for k in idp.published]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/jwks"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.add_key("k1", publish=True)

    def add_key(self, kid: str, publish: bool) -> None:
        self.keys[kid] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if publish:
            self.published.append(kid)

    def jwk(self, kid: str) -> dict:
        d = jwt.algorithms.RSAAlgorithm.to_jwk(self.keys[kid].public_key(), as_dict=True)
        return {**d, "kid": kid, "use": "sig", "alg": "RS256"}

    def token(self, kid: str = "k1", *, sub: str = "alice", org: str = "org-acme", groups=("eng",),
              aud: str = "uar", iss: str = ISSUER, exp_in: int = 300, key: rsa.RSAPrivateKey | None = None) -> str:
        now = int(time.time())
        claims = {"iss": iss, "aud": aud, "sub": sub, "iat": now, "exp": now + exp_in, "org": org,
                  "groups": list(groups)}
        pem = (key or self.keys[kid]).private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption())
        return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": kid})


def governance_settings(idp: Idp):
    def customize(d: dict) -> None:
        d["auth"]["oidc"] = [{
            "issuer": ISSUER, "audience": "uar", "jwks_url": idp.url, "tenant_claim": "org",
            "tenant_map": {"org-acme": "acme"},
            "group_roles": {"eng": ["developer"], "approvers": ["approver"]},
            "subject_roles": {"svc-ci": ["developer"]}}]
        d["tool_policies"] = [
            {"tool": "fs.write_text", "effect": "require_approval", "roles": ["developer", "admin"],
             "args": {"path": {"prefix": ["reports/"]}}, "approver_roles": ["approver"]},
            {"tool": "fs.*", "roles": ["developer", "admin", "operator"]},
            {"tool": "db.*", "roles": ["developer", "admin"]},
        ]
        d["redaction"] = {"builtin": ["email", "card_number"], "egress_classes": ["cloud"]}
        d["approvals"] = {"default_ttl_s": 600}
    return customize


@pytest.fixture(scope="module")
def idp():
    i = Idp()
    yield i
    i.server.shutdown()


@pytest.fixture(scope="module")
async def gov(tmp_path_factory, idp):
    name = f"uar_test_gov_{os.getpid()}"
    e, cleanup = await start_env(tmp_path_factory.mktemp("gov"), name, customize=governance_settings(idp))
    yield e
    await stop_env(e, cleanup, name)


# ------------------------------------------------------------------ helpers

def agent(aid: str, nodes: list, edges: list, *, perms=None, version="1.0.0"):
    return {"apiVersion": "uar/v1", "kind": "Agent", "metadata": {"id": aid, "version": version},
            "spec": {"start": nodes[0]["id"], "nodes": nodes, "edges": edges,
                     "permissions": perms or {"models": ["local:*"], "tools": [], "agents": []}}}


EXPENSE = agent("expense", [
    {"id": "approve", "type": "approval", "action": "expense.pay", "description": "Pay a supplier invoice",
     "args": {"amount": "${input.amount}", "payee": "${input.payee}"}, "summary": {"requested_for": "${input.payee}"},
     "approvers": ["finance"], "on_reject": "continue"},
    {"id": "paid", "type": "return",
     "value": {"result": "paid", "payee": "${input.payee}", "by": "${nodes.approve.output.decided_by}"}},
    {"id": "denied", "type": "return", "value": {"result": "denied", "status": "${nodes.approve.output.status}"}},
], [{"from": "approve", "to": "paid", "when": "${nodes.approve.output.approved}"},
    {"from": "approve", "to": "denied", "when": "${!nodes.approve.output.approved}"}])

STRICT = agent("strict_gate", [
    {"id": "gate", "type": "approval", "action": "deploy.prod", "args": {"service": "${input.service}"},
     "expires_in_s": 1},
    {"id": "done", "type": "return", "value": {"deployed": "${input.service}"}},
], [{"from": "gate", "to": "done"}])

PUBLISH = agent("publish_report", [
    {"id": "write", "type": "tool", "tool": "fs.write_text",
     "args": {"path": "${input.path}", "content": "${input.text}"}},
    {"id": "done", "type": "return", "value": {"written": "${input.path}"}},
], [{"from": "write", "to": "done"}], perms={"models": [], "tools": ["fs.write_text"], "agents": []})

CHILD = agent("child_gate", [
    {"id": "gate", "type": "approval", "action": "child.step", "args": {"x": "${input.x}"}},
    {"id": "out", "type": "return", "value": {"child": "${input.x}"}},
], [{"from": "gate", "to": "out"}])

PARENT = agent("parent_gate", [
    {"id": "call", "type": "agent", "agent": "child_gate", "input": {"x": "${input.x}"}},
    {"id": "out", "type": "return", "value": {"parent": "${nodes.call.output.child}"}},
], [{"from": "call", "to": "out"}], perms={"models": [], "tools": [], "agents": ["child_gate"]})

SIMPLE = agent("simple", [{"id": "r", "type": "return", "value": {"ok": True}}], [])


def client(env, key: str | None = None, bearer: str | None = None) -> httpx.AsyncClient:
    h = headers(key) if key else {"Authorization": f"Bearer {bearer}"}
    return httpx.AsyncClient(base_url=env.http, headers=h, timeout=30)


async def start(env, agent_id: str, input_: dict, key: str | None = None) -> str:
    async with client(env, key or env.keys.dev) as c:
        r = await c.post("/api/v1/agent/run", json={"agent_id": agent_id, "input": input_})
        assert r.status_code == 202, r.text
        return r.json()["run_id"]


async def wait_status(env, rid: str, statuses: set[str], timeout: float = 20) -> dict:
    async with client(env, env.keys.admin) as c:
        for _ in range(int(timeout / 0.1)):
            run = (await c.get(f"/api/v1/runs/{rid}")).json()
            if run["status"] in statuses:
                return run
            await asyncio.sleep(0.1)
    raise AssertionError(f"run {rid} never reached {statuses}: {run}")


async def pending_for(env, rid: str, key: str | None = None) -> dict:
    async with client(env, key or env.keys.approver) as c:
        r = await c.get("/api/v1/approvals", params={"status": "pending", "run_id": rid})
        assert r.status_code == 200, r.text
        items = r.json()["approvals"]
        assert len(items) == 1, items
        return items[0]


async def decide(env, key: str, approval_id: str, approve: bool, **extra) -> httpx.Response:
    async with client(env, key) as c:
        return await c.post(f"/api/v1/approvals/{approval_id}/decision", json={"approve": approve, **extra})


@pytest.fixture(scope="module", autouse=True)
async def agents(gov):
    async with client(gov, gov.keys.dev) as c:
        for d in (EXPENSE, STRICT, PUBLISH, CHILD, PARENT, SIMPLE):
            r = await c.post("/api/v1/agents", json={"definition": d})
            assert r.status_code in (200, 201), r.text


# ------------------------------------------------------------------ approval nodes

async def test_approval_node_approved_path_and_no_replay(gov):
    rid = await start(gov, "expense", {"amount": 120, "payee": "Initech"})
    run = await wait_status(gov, rid, {"waiting_approval"})
    assert run["current_node"] == "approve"
    events = await gov.svc.store.fetchall("SELECT body FROM run_events WHERE run_id=%s ORDER BY seq", rid)
    req = [e["body"]["approval_required"] for e in events if "approval_required" in e["body"]]
    assert req and req[0]["action"] == "expense.pay" and len(req[0]["args_hash"]) == 64

    a = await pending_for(gov, rid)
    assert a["kind"] == "node" and a["action"] == "expense.pay" and a["approver_roles"] == ["finance"]
    assert a["summary"]["args"] == {"amount": 120, "payee": "Initech"} and a["requested_by"] == "dev@acme"
    assert a["summary"]["requested_for"] == "Initech"

    async with client(gov, gov.keys.dev) as c:   # the requester has no approval permissions at all
        assert (await c.get("/api/v1/approvals")).status_code == 403
    assert (await decide(gov, gov.keys.dev, a["approval_id"], True)).status_code == 403
    r = await decide(gov, gov.keys.approver, a["approval_id"], True)   # approver, but not in role finance
    assert r.status_code == 403 and "finance" in r.text
    r = await decide(gov, gov.keys.finance, a["approval_id"], True, args_hash="0" * 64)
    assert r.status_code == 409 and "args_hash" in r.text

    r = await decide(gov, gov.keys.finance, a["approval_id"], True, args_hash=a["args_hash"], comment="ok")
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    assert r.json()["decided_by"] == "cfo@acme"
    run = await wait_status(gov, rid, {"succeeded", "failed"})
    assert run["status"] == "succeeded" and run["output"] == {"result": "paid", "payee": "Initech", "by": "cfo@acme"}

    r = await decide(gov, gov.keys.finance, a["approval_id"], False)   # decided once: no replay
    assert r.status_code == 409 and r.json()["error"]["details"]["status"] == "approved"
    async with client(gov, gov.keys.approver) as c:
        got = (await c.get(f"/api/v1/approvals/{a['approval_id']}")).json()
    assert got["consumed"] is True and got["status"] == "approved"


async def test_approval_node_rejected_path(gov):
    rid = await start(gov, "expense", {"amount": 99999, "payee": "Shady Ltd"})
    await wait_status(gov, rid, {"waiting_approval"})
    a = await pending_for(gov, rid)
    r = await decide(gov, gov.keys.finance, a["approval_id"], False, comment="not a known supplier")
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    run = await wait_status(gov, rid, {"succeeded", "failed"})
    assert run["output"] == {"result": "denied", "status": "rejected"}


async def test_unanswered_approval_expires_and_fails_closed(gov):
    rid = await start(gov, "strict_gate", {"service": "billing"})
    await wait_status(gov, rid, {"waiting_approval"})
    a = await pending_for(gov, rid)
    run = await wait_status(gov, rid, {"failed", "succeeded"}, timeout=15)
    assert run["status"] == "failed" and run["error"]["code"] == "policy_denied" and "expired" in run["error"]["message"]
    r = await decide(gov, gov.keys.approver, a["approval_id"], True)
    assert r.status_code == 409 and r.json()["error"]["details"]["status"] == "expired"


async def test_requester_cannot_approve_own_run(gov):
    rid = await start(gov, "child_gate", {"x": 1}, key=gov.keys.admin)
    await wait_status(gov, rid, {"waiting_approval"})
    a = await pending_for(gov, rid)
    r = await decide(gov, gov.keys.admin, a["approval_id"], True)   # admin started it: separation of duties
    assert r.status_code == 403 and "cannot decide" in r.text
    assert (await decide(gov, gov.keys.approver, a["approval_id"], True)).status_code == 200
    assert (await wait_status(gov, rid, {"succeeded", "failed"}))["status"] == "succeeded"


async def test_sub_agent_approval_parks_and_resumes_parent(gov):
    rid = await start(gov, "parent_gate", {"x": 7})
    await wait_status(gov, rid, {"waiting_approval"})
    async with client(gov, gov.keys.approver) as c:
        items = (await c.get("/api/v1/approvals", params={"status": "pending"})).json()["approvals"]
    a = next(i for i in items if i["action"] == "child.step" and i["summary"]["args"] == {"x": 7})
    assert a["run_id"] != rid   # the approval belongs to the sub-run
    assert (await decide(gov, gov.keys.approver, a["approval_id"], True)).status_code == 200
    run = await wait_status(gov, rid, {"succeeded", "failed"})
    assert run["status"] == "succeeded" and run["output"] == {"parent": 7}


async def test_cancel_waiting_run_cancels_its_approval(gov):
    rid = await start(gov, "expense", {"amount": 5, "payee": "Cancel Co"})
    await wait_status(gov, rid, {"waiting_approval"})
    a = await pending_for(gov, rid)
    async with client(gov, gov.keys.dev) as c:
        r = await c.post(f"/api/v1/runs/{rid}/cancel", json={"reason": "no longer needed"})
        assert r.status_code == 200 and r.json()["status"] == "cancelled", r.text
    async with client(gov, gov.keys.approver) as c:
        assert (await c.get(f"/api/v1/approvals/{a['approval_id']}")).json()["status"] == "cancelled"
    assert (await decide(gov, gov.keys.finance, a["approval_id"], True)).status_code == 409


# ------------------------------------------------------------------ inline tool approvals

async def test_tool_call_waits_for_approval_of_its_exact_arguments(gov):
    target = gov.ws / "reports" / "q3.md"
    rid = await start(gov, "publish_report", {"path": "reports/q3.md", "text": "Q3 revenue up 4%"})
    await wait_status(gov, rid, {"waiting_approval"})
    assert not target.exists()                                  # nothing ran before the decision
    a = await pending_for(gov, rid)
    assert a["kind"] == "tool" and a["action"] == "fs.write_text" and a["approver_roles"] == ["approver"]
    assert a["summary"]["args"] == {"path": "reports/q3.md", "content": "Q3 revenue up 4%"}
    assert (await decide(gov, gov.keys.approver, a["approval_id"], True)).status_code == 200
    run = await wait_status(gov, rid, {"succeeded", "failed", "needs_attention"})
    assert run["status"] == "succeeded", run
    assert target.read_text(encoding="utf-8") == "Q3 revenue up 4%"
    async with client(gov, gov.keys.approver) as c:
        assert (await c.get(f"/api/v1/approvals/{a['approval_id']}")).json()["consumed"] is True
    audit = await gov.svc.store.fetchall("SELECT details FROM audit WHERE tenant='acme' AND run_id=%s AND "
                                         "action='tool.execute' AND outcome='intent'", rid)
    assert any(a["approval_id"] in r["details"]["policy"] for r in audit)


async def test_rejected_tool_call_never_runs(gov):
    target = gov.ws / "reports" / "rejected.md"
    rid = await start(gov, "publish_report", {"path": "reports/rejected.md", "text": "nope"})
    await wait_status(gov, rid, {"waiting_approval"})
    a = await pending_for(gov, rid)
    assert (await decide(gov, gov.keys.approver, a["approval_id"], False)).status_code == 200
    run = await wait_status(gov, rid, {"succeeded", "failed"})
    assert run["status"] == "failed" and run["error"]["code"] == "policy_denied"
    assert not target.exists()


async def test_approval_gated_tool_cannot_be_called_directly(gov):
    async with client(gov, gov.keys.dev) as c:
        r = await c.post("/api/v1/tool/execute", json={"tool": "fs.write_text",
                                                        "args": {"path": "reports/direct.md", "content": "x"}})
    assert r.status_code == 403 and r.json()["error"]["code"] == "policy_denied" and "approved" in r.text
    assert not (gov.ws / "reports" / "direct.md").exists()
    # A policy denial is still decided before any human is asked.
    rid = await start(gov, "publish_report", {"path": "../escape.md", "text": "x"})
    run = await wait_status(gov, rid, {"failed", "waiting_approval"})
    assert run["status"] == "failed" and run["error"]["code"] == "policy_denied"


async def test_approval_consumed_once(gov):
    """An approved tool approval authorises one call: consuming it again fails."""
    rid = await start(gov, "publish_report", {"path": "reports/once.md", "text": "1"})
    await wait_status(gov, rid, {"waiting_approval"})
    a = await pending_for(gov, rid)
    await decide(gov, gov.keys.approver, a["approval_id"], True)
    await wait_status(gov, rid, {"succeeded"})
    assert not await gov.svc.approvals.consume("acme", a["approval_id"], rid, "fs.write_text", a["args_hash"])


async def test_dry_run_shows_approval_paths(gov):
    async with client(gov, gov.keys.dev) as c:
        r = await c.post("/api/v1/dry-run", json={"agent_id": "expense", "input": {"amount": 1, "payee": "x"}})
        rep = r.json()
        assert rep["valid"] and any("pauses the run" in w for w in rep["warnings"]), rep
        step = next(s for s in rep["steps"] if s["node_id"] == "approve")
        assert step["detail"] == "approval of expense.pay by finance"
        r = await c.post("/api/v1/dry-run", json={"agent_id": "expense", "mode": "simulate",
                                                   "input": {"amount": 1, "payee": "x"},
                                                   "fixtures": {"approve": {"approved": False}}})
        rep = r.json()
        assert not rep["errors"] and rep["branches"] == ["approve -> denied"], rep
        r = await c.post("/api/v1/dry-run", json={"agent_id": "publish_report",
                                                   "input": {"path": "reports/a.md", "text": "t"}})
        assert any("requires a human approval" in w for w in r.json()["warnings"])


# ------------------------------------------------------------------ audit chain

async def test_audit_chain_verifies_and_exports(gov):
    async with client(gov, gov.keys.dev) as c:
        assert (await c.get("/api/v1/audit/verify")).status_code == 403
    async with client(gov, gov.keys.auditor) as c:
        v = (await c.get("/api/v1/audit/verify")).json()
        assert v["ok"] and v["rows"] > 5 and v["first_seq"] == 1 and v["broken_at_seq"] in (0, "0"), v
        after, recs = 0, []
        while True:
            page = (await c.get("/api/v1/audit/export", params={"after_seq": after, "limit": 7})).json()
            if not page["records"]:
                break
            recs += page["records"]
            after = int(page["next_after_seq"])
    seqs = [int(r["seq"]) for r in recs]
    assert seqs == list(range(1, len(seqs) + 1))
    assert all(b["prev_hash"] == a["hash"] for a, b in zip(recs, recs[1:]))
    assert {r["action"] for r in recs} >= {"agents.register", "approvals.request", "approvals.decide"}


async def test_audit_is_append_only_and_tampering_is_detected(gov):
    store, audit = gov.svc.store, gov.svc.audit
    # Build a small chain for a tenant nobody else writes to during this test.
    p = Principal("cloudco", "dev@cloudco", ("developer",))
    for i in range(4):
        await audit.record(p, "test.event", f"t{i}", "ok", details={"i": i})
    assert (await audit.verify("cloudco"))["ok"]
    with pytest.raises(Exception, match="append-only"):
        await store.execute("UPDATE audit SET outcome='x' WHERE tenant='cloudco'")
    with pytest.raises(Exception, match="append-only"):
        await store.execute("DELETE FROM audit WHERE tenant='cloudco'")

    async def bypass(sql: str, *args):   # a database owner disabling the trigger: the chain still tells
        async with store.tx() as c:
            await c.execute("ALTER TABLE audit DISABLE TRIGGER audit_append_only")
            await c.execute(sql, args)
            await c.execute("ALTER TABLE audit ENABLE TRIGGER audit_append_only")

    original = (await store.fetchone("SELECT outcome FROM audit WHERE tenant='cloudco' AND seq=2"))["outcome"]
    await bypass("UPDATE audit SET outcome='tampered' WHERE tenant='cloudco' AND seq=2")
    v = await audit.verify("cloudco")
    assert not v["ok"] and v["broken_at_seq"] == 2 and "modified" in v["problem"]
    await bypass("UPDATE audit SET outcome=%s WHERE tenant='cloudco' AND seq=2", original)
    assert (await audit.verify("cloudco"))["ok"]

    row = await store.fetchone("SELECT * FROM audit WHERE tenant='cloudco' AND seq=3")
    await bypass("DELETE FROM audit WHERE tenant='cloudco' AND seq=3")
    v = await audit.verify("cloudco")
    assert not v["ok"] and v["broken_at_seq"] == 3 and "missing" in v["problem"]
    last = await store.fetchone("SELECT * FROM audit WHERE tenant='cloudco' ORDER BY seq DESC LIMIT 1")
    cols = list(row)
    await bypass(f"INSERT INTO audit ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                 *[json.dumps(row[k]) if k == "details" else row[k] for k in cols])
    assert (await audit.verify("cloudco"))["ok"]
    await bypass("DELETE FROM audit WHERE tenant='cloudco' AND seq=%s", last["seq"])
    v = await audit.verify("cloudco")
    assert not v["ok"] and "removed from the end" in v["problem"]
    await bypass(f"INSERT INTO audit ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                 *[json.dumps(last[k]) if k == "details" else last[k] for k in cols])
    assert (await audit.verify("cloudco"))["ok"]


# ------------------------------------------------------------------ cross-tenant isolation

async def test_other_tenant_cannot_see_or_decide_approvals(gov):
    rid = await start(gov, "expense", {"amount": 3, "payee": "Isolation Inc"})
    await wait_status(gov, rid, {"waiting_approval"})
    a = await pending_for(gov, rid)
    async with client(gov, gov.keys.globex_admin) as c:
        assert (await c.get(f"/api/v1/approvals/{a['approval_id']}")).status_code == 404
        listed = (await c.get("/api/v1/approvals")).json()["approvals"]
        assert all(x["approval_id"] != a["approval_id"] for x in listed)
        r = await c.post(f"/api/v1/approvals/{a['approval_id']}/decision", json={"approve": True})
        assert r.status_code == 404
        assert (await c.get(f"/api/v1/runs/{rid}")).status_code == 404
        v = (await c.get("/api/v1/audit/verify")).json()
        exported = (await c.get("/api/v1/audit/export", params={"limit": 1000})).json()["records"]
    assert v["ok"]
    assert all(r["actor"].endswith("@globex") or r["actor"].startswith("system:") for r in exported),         {r["actor"] for r in exported}
    await decide(gov, gov.keys.finance, a["approval_id"], False)
    await wait_status(gov, rid, {"succeeded"})


async def test_other_tenant_runs_do_not_reuse_approvals(gov):
    """An approval id is bound to its tenant and run: consuming it from another tenant or run fails."""
    rid = await start(gov, "publish_report", {"path": "reports/bound.md", "text": "b"})
    await wait_status(gov, rid, {"waiting_approval"})
    a = await pending_for(gov, rid)
    await decide(gov, gov.keys.approver, a["approval_id"], True)
    await wait_status(gov, rid, {"succeeded"})
    ok = await gov.svc.approvals.consume("globex", a["approval_id"], rid, "fs.write_text", a["args_hash"])
    assert not ok
    ok = await gov.svc.approvals.consume("acme", a["approval_id"], "run_other", "fs.write_text", a["args_hash"])
    assert not ok


# ------------------------------------------------------------------ redaction

async def test_prompts_to_cloud_models_are_redacted(gov):
    text = "mail bob@example.com, card 4111 1111 1111 1111, order 1234 5678"
    async with client(gov, gov.keys.cloud_dev) as c:
        r = await c.post("/api/v1/inference", json={"model": "cloud:fakecloud/big", "input": text})
        assert r.status_code == 200, r.text
        content = r.json()["content"]
    assert "bob@example.com" not in content and "[redacted:email]" in content
    assert "[redacted:card_number]" in content and "1234 5678" in content   # not a valid card number
    async with client(gov, gov.keys.dev) as c:   # local models are not an egress class here
        r = await c.post("/api/v1/inference", json={"model": "local:fake/echo", "input": text})
        assert "bob@example.com" in r.json()["content"]
    row = await gov.svc.store.fetchone("SELECT details FROM audit WHERE tenant='cloudco' AND action='model.invoke' "
                                       "AND outcome='intent' ORDER BY seq DESC LIMIT 1")
    assert row["details"]["redacted"] is True


async def test_event_log_and_audit_are_redacted(gov):
    rid = await start(gov, "expense", {"amount": 10, "payee": "carol@example.com"})
    await wait_status(gov, rid, {"waiting_approval"})
    a = await pending_for(gov, rid)
    assert a["summary"]["args"]["payee"] == "[redacted:email]"   # what approvers see is redacted too
    await decide(gov, gov.keys.finance, a["approval_id"], True, comment="confirmed with carol@example.com")
    run = await wait_status(gov, rid, {"succeeded"})
    assert run["output"]["payee"] == "carol@example.com"          # the result itself is not altered
    events = await gov.svc.store.fetchall("SELECT body FROM run_events WHERE run_id=%s", rid)
    blob = json.dumps([e["body"] for e in events])
    assert "carol@example.com" not in blob and "[redacted:email]" in blob
    row = await gov.svc.store.fetchone("SELECT details FROM audit WHERE action='approvals.decide' AND target=%s",
                                       a["approval_id"])
    assert row["details"]["comment"] == "confirmed with [redacted:email]"


# ------------------------------------------------------------------ OIDC / JWKS

async def test_oidc_group_and_service_account_mapping(gov, idp):
    async with client(gov, bearer=idp.token(groups=["eng"])) as c:
        assert (await c.get("/api/v1/models")).status_code == 200
        r = await c.post("/api/v1/agent/run", json={"agent_id": "simple", "input": {}})
        assert r.status_code == 202, r.text
        run = await wait_status(gov, r.json()["run_id"], {"succeeded", "failed"})
        assert run["status"] == "succeeded"
        assert (await c.get("/api/v1/approvals")).status_code == 403   # engineers are not approvers
    async with client(gov, bearer=idp.token(sub="bob", groups=["approvers"])) as c:
        assert (await c.get("/api/v1/approvals")).status_code == 200
        assert (await c.get("/api/v1/models")).status_code == 403
    async with client(gov, bearer=idp.token(sub="svc-ci", groups=[])) as c:   # client-credentials token
        assert (await c.get("/api/v1/models")).status_code == 200
    p = gov.svc.auth.authenticate(None, idp.token(sub="svc-ci", groups=[]))
    assert (p.tenant, p.subject, p.roles, p.key_id) == ("acme", "svc-ci", ("developer",), "oidc")


@pytest.mark.parametrize("kwargs, reason", [
    ({"aud": "someone-else"}, "InvalidAudienceError"),
    ({"exp_in": -120}, "ExpiredSignatureError"),
    ({"org": "org-unknown"}, None),
    ({"iss": "https://evil.example/"}, None),
])
async def test_oidc_rejects_bad_tokens(gov, idp, kwargs, reason):
    async with client(gov, bearer=idp.token(**kwargs)) as c:
        r = await c.get("/api/v1/models")
    assert r.status_code == 401, r.text
    if reason:
        assert r.json()["error"]["details"]["reason"] == reason


async def test_oidc_rejects_forged_and_alg_confused_tokens(gov, idp):
    forged = idp.token(key=rsa.generate_private_key(public_exponent=65537, key_size=2048))  # right kid, wrong key
    pub = idp.keys["k1"].public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)
    claims = jwt.decode(idp.token(), options={"verify_signature": False})
    import hmac, hashlib, base64   # HS256 signed with the public key as secret (algorithm confusion)
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode())
    body = b64(json.dumps(claims).encode())
    confused = f"{head}.{body}." + b64(hmac.new(pub, f"{head}.{body}".encode(), hashlib.sha256).digest())
    for tok in (forged, confused):
        async with client(gov, bearer=tok) as c:
            assert (await c.get("/api/v1/models")).status_code == 401


async def test_oidc_key_rotation_refreshes_jwks(gov, idp):
    cache = gov.svc.auth.oidc[ISSUER]
    cache.MIN_REFRESH_S = 0
    idp.add_key("k2", publish=True)
    tok = idp.token("k2")
    async with client(gov, bearer=tok) as c:
        r = await c.get("/api/v1/models")
        if r.status_code == 401:   # first sight of the new kid triggers a background refresh
            assert r.json()["error"]["details"]["reason"] == "unknown_kid"
            for _ in range(50):
                await asyncio.sleep(0.1)
                r = await c.get("/api/v1/models")
                if r.status_code == 200:
                    break
        assert r.status_code == 200, r.text


# ------------------------------------------------------------------ retention (runs last: prunes this env)

async def test_retention_prunes_and_audit_chain_still_verifies(gov):
    s, store = gov.svc.s, gov.svc.store
    waiting = await start(gov, "expense", {"amount": 1, "payee": "Keep Me"})
    await wait_status(gov, waiting, {"waiting_approval"})
    done = await store.fetchone("SELECT count(*) AS n FROM runs WHERE status IN ('succeeded','failed','cancelled')")
    assert done["n"] > 0
    s.retention.runs_days = s.retention.usage_days = s.retention.audit_days = 30
    out = await gov.svc.retention.run_once(now=datetime.now(timezone.utc) + timedelta(days=31))
    assert out["runs"] >= done["n"] and out["audit"] > 0
    left = await store.fetchall("SELECT run_id, status FROM runs WHERE parent_run_id IS NULL")
    assert waiting in [r["run_id"] for r in left if r["status"] == "waiting_approval"]
    assert all(r["status"] not in ("succeeded", "failed", "cancelled") for r in left)
    for tenant in ("acme", "cloudco"):
        v = await gov.svc.audit.verify(tenant)
        assert v["ok"] and v["anchor_seq"] > 0, (tenant, v)
    # New rows continue the chain after the anchor.
    async with client(gov, gov.keys.auditor) as c:
        v = (await c.get("/api/v1/audit/verify")).json()
    assert v["ok"] and int(v["first_seq"]) == int(v["anchor_seq"]) + 1
    kinds = await store.fetchall("SELECT action FROM audit WHERE tenant='acme' AND action='retention.delete'")
    assert kinds
