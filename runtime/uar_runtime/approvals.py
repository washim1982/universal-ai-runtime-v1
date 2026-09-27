"""Human approvals for agent runs.

Two sources:
- an `approval` node in a graph (kind "node"): the run pauses until someone decides; the node's
  output is {approved, status, approval_id, decided_by, comment};
- a tool policy with effect `require_approval` (kind "tool"): the tool node pauses before the call
  and the call runs only after its exact arguments were approved.

Guarantees:
- An approval is bound to (tenant, run, node, action, sha256 of the arguments). A decision on it
  cannot be reused for other arguments, another node or another run.
- Decided once: only while pending and unexpired (pending -> approved | rejected | expired | cancelled).
- Consumed once: an approved action is performed at most once (`consumed_at` is set atomically).
- Separation of duties: the principal that started the run cannot decide (unless configured), the
  approver needs approvals:decide in the same tenant and one of the approval's approver roles.
- Fail closed: an unanswered approval expires, and an expired approval is a rejection.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .config import Settings
from .errors import UARError, redact
from .governance import Audit, Principal
from .redaction import Redactor
from .store import Store, jsonb

log = logging.getLogger("uar.approvals")


class WaitForApproval(Exception):
    """Raised inside a run: park it (status waiting_approval) until the approval is decided."""

    def __init__(self, approval: dict):
        super().__init__(approval["approval_id"])
        self.approval = approval


def _iso(v: datetime | None) -> str:
    return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if v else ""


def approval_view(row: dict) -> dict:
    return {"approval_id": row["approval_id"], "status": row["status"], "run_id": row["run_id"],
            "node_id": row["node_id"], "kind": row["kind"], "action": row["action"], "args_hash": row["args_hash"],
            "summary": row["summary"] or {}, "approver_roles": list(row["approver_roles"] or []),
            "requested_by": row["requested_by"], "decided_by": row["decided_by"] or "",
            "comment": row["comment"] or "", "created_at": _iso(row["created_at"]),
            "expires_at": _iso(row["expires_at"]), "decided_at": _iso(row["decided_at"]),
            "consumed": row["consumed_at"] is not None}


# Runs in the same tree as a run (itself, its ancestors and descendants) that are parked.
_TREE = """
WITH RECURSIVE up AS (
    SELECT run_id, parent_run_id FROM runs WHERE run_id = %(run)s
    UNION ALL SELECT r.run_id, r.parent_run_id FROM runs r JOIN up ON r.run_id = up.parent_run_id
), root AS (SELECT run_id FROM up WHERE parent_run_id IS NULL),
down AS (
    SELECT run_id, parent_run_id FROM runs WHERE run_id = (SELECT run_id FROM root)
    UNION ALL SELECT r.run_id, r.parent_run_id FROM runs r JOIN down ON r.parent_run_id = down.run_id
)
"""


class Approvals:
    def __init__(self, settings: Settings, store: Store, audit: Audit, redactor: Redactor):
        self.s = settings
        self.store = store
        self.audit = audit
        self.redactor = redactor
        self.on_enqueue: Callable[[], None] = lambda: None

    # ------------------------------------------------------------ inside a run

    async def gate(self, run: dict, node_id: str, kind: str, action: str, args_hash: str, summary: Any,
                   approver_roles: list[str], ttl_s: float | None, reuse_id: str | None) -> dict:
        """Return the decided approval for this exact action, or raise WaitForApproval.

        `reuse_id` is the approval the run was parked on (from its checkpoint); it is used only when it
        is bound to the same node, action and arguments. Anything else gets a new approval.
        """
        if reuse_id:
            row = await self.store.fetchone("SELECT * FROM approvals WHERE approval_id=%s AND tenant=%s",
                                            reuse_id, run["tenant"])
            if row and (row["run_id"], row["node_id"], row["action"], row["args_hash"]) == \
                    (run["run_id"], node_id, action, args_hash):
                if row["status"] == "pending":
                    raise WaitForApproval(row)
                return row
        # After a crash between creating the approval and parking the run, reuse the open one.
        row = await self.store.fetchone("SELECT * FROM approvals WHERE run_id=%s AND node_id=%s AND status='pending'",
                                        run["run_id"], node_id)
        if row is not None:
            if (row["action"], row["args_hash"]) == (action, args_hash):
                raise WaitForApproval(row)
            await self.store.execute("UPDATE approvals SET status='cancelled', decided_at=now(), comment=%s "
                                     "WHERE approval_id=%s AND status='pending'",
                                     "superseded: the action or its arguments changed", row["approval_id"])
        ttl = min(float(ttl_s or self.s.approvals.default_ttl_s), self.s.approvals.max_ttl_s)
        aid = "apr_" + secrets.token_hex(10)
        summary = self.redactor.value(redact(summary)) if self.redactor.active else redact(summary)
        row = await self.store.fetchone(
            "INSERT INTO approvals (approval_id, tenant, run_id, node_id, kind, action, args_hash, summary, "
            "approver_roles, requested_by, status, expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s) "
            "RETURNING *", aid, run["tenant"], run["run_id"], node_id, kind, action, args_hash, jsonb(summary),
            list(approver_roles), run["principal"]["subject"],
            datetime.now(timezone.utc) + timedelta(seconds=ttl))
        p = Principal(run["tenant"], run["principal"]["subject"], tuple(run["principal"].get("roles", [])))
        await self.audit.record(p, "approvals.request", aid, "pending", run_id=run["run_id"],
                                request_id=run.get("request_id") or "",
                                details={"node": node_id, "kind": kind, "action": action, "args_hash": args_hash,
                                         "expires_in_s": ttl})
        raise WaitForApproval(row)

    async def consume(self, tenant: str, approval_id: str, run_id: str, action: str, args_hash: str) -> bool:
        """Mark an approved approval as used. False if it is not approved for exactly this call, or used."""
        row = await self.store.fetchone(
            "UPDATE approvals SET consumed_at=now() WHERE approval_id=%s AND tenant=%s AND run_id=%s AND action=%s "
            "AND args_hash=%s AND status='approved' AND consumed_at IS NULL RETURNING approval_id",
            approval_id, tenant, run_id, action, args_hash)
        return row is not None

    # ------------------------------------------------------------ API

    async def get(self, p: Principal, approval_id: str) -> dict:
        if not (p.has("approvals:read") or p.has("approvals:decide")):
            raise UARError("permission_denied", "missing permission approvals:read")
        row = await self.store.fetchone("SELECT * FROM approvals WHERE approval_id=%s AND tenant=%s",
                                        approval_id, p.tenant)
        if row is None:
            raise UARError("not_found", f"approval {approval_id} not found")
        return approval_view(row)

    async def list(self, p: Principal, status: str = "", run_id: str = "", limit: int = 200) -> dict:
        if not (p.has("approvals:read") or p.has("approvals:decide")):
            raise UARError("permission_denied", "missing permission approvals:read")
        if status and status not in ("pending", "approved", "rejected", "expired", "cancelled"):
            raise UARError("invalid_argument", "status must be pending, approved, rejected, expired or cancelled")
        rows = await self.store.fetchall(
            "SELECT * FROM approvals WHERE tenant=%s AND (%s = '' OR status=%s) AND (%s = '' OR run_id=%s) "
            "ORDER BY created_at DESC LIMIT %s", p.tenant, status, status, run_id, run_id, limit)
        return {"approvals": [approval_view(r) for r in rows]}

    async def decide(self, p: Principal, approval_id: str, approve: bool, comment: str, args_hash: str,
                     request_id: str) -> dict:
        p.require("approvals:decide")
        row = await self.store.fetchone("SELECT * FROM approvals WHERE approval_id=%s AND tenant=%s",
                                        approval_id, p.tenant)
        if row is None:
            raise UARError("not_found", f"approval {approval_id} not found")
        if row["status"] != "pending":
            raise UARError("failed_precondition", f"approval is already {row['status']}",
                           details={"status": row["status"]})
        if row["expires_at"] <= datetime.now(timezone.utc):
            await self.expire_due()
            raise UARError("failed_precondition", "approval has expired", details={"status": "expired"})
        if args_hash and args_hash != row["args_hash"]:
            raise UARError("failed_precondition", "args_hash does not match the approval (the arguments differ "
                           "from what was reviewed)")
        roles = list(row["approver_roles"] or [])
        if roles and not set(roles) & set(p.roles):
            raise UARError("permission_denied", f"approver needs one of the roles {roles}",
                           details={"approver_roles": roles})
        if p.subject == row["requested_by"] and not self.s.approvals.allow_self_approval:
            raise UARError("permission_denied", "the principal that started the run cannot decide its approvals")
        status = "approved" if approve else "rejected"
        await self.audit.record(p, "approvals.decide", approval_id, status, request_id=request_id,
                                run_id=row["run_id"], details={"action": row["action"], "args_hash": row["args_hash"],
                                                               "comment": comment[:500]})
        async with self.store.tx() as c:
            cur = await c.execute(
                "UPDATE approvals SET status=%s, decided_by=%s, decided_at=now(), comment=%s WHERE approval_id=%s "
                "AND tenant=%s AND status='pending' AND expires_at > now() RETURNING *",
                (status, p.subject, comment[:2000], approval_id, p.tenant))
            decided = await cur.fetchone()
            if decided is None:   # decided or expired concurrently
                raise UARError("failed_precondition", "approval is no longer pending")
            await self._wake_tree(c, row["run_id"])
        self.on_enqueue()
        return approval_view(decided)

    # ------------------------------------------------------------ maintenance

    async def expire_due(self) -> int:
        """Expire pending approvals past their deadline and resume their runs (which then fail closed)."""
        async with self.store.tx() as c:
            cur = await c.execute("UPDATE approvals SET status='expired', decided_at=now() WHERE status='pending' "
                                  "AND expires_at <= now() RETURNING run_id")
            rows = await cur.fetchall()
            for r in rows:
                await self._wake_tree(c, r["run_id"])
        if rows:
            self.on_enqueue()
        return len(rows)

    async def _wake_tree(self, c, run_id: str) -> None:
        """Requeue the parked root run; parked sub-runs become resumable by their parent."""
        await c.execute(_TREE + "UPDATE runs SET status = CASE WHEN parent_run_id IS NULL THEN 'queued' ELSE "
                        "'running' END, updated_at=now() WHERE run_id IN (SELECT run_id FROM down) "
                        "AND status='waiting_approval'", {"run": run_id})

    async def cancel_tree(self, c, run_id: str) -> None:
        """Cancel parked runs of the tree and their open approvals (used when a waiting run is cancelled)."""
        await c.execute(_TREE + "UPDATE approvals SET status='cancelled', decided_at=now(), comment='run cancelled' "
                        "WHERE run_id IN (SELECT run_id FROM down) AND status='pending'", {"run": run_id})
        await c.execute(_TREE + "UPDATE runs SET status='cancelled', cancel_requested=true, updated_at=now(), "
                        "lease_owner=NULL, lease_expires_at=NULL, error=%(err)s "
                        "WHERE run_id IN (SELECT run_id FROM down) AND status='waiting_approval'",
                        {"run": run_id, "err": jsonb({"code": "cancelled",
                                                      "message": "cancelled while waiting for approval"})})
