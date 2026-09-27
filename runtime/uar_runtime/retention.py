"""Retention and deletion jobs.

Runs on every worker, but each cycle is guarded by a PostgreSQL advisory lock so only one replica
prunes at a time. What is deleted, per configured age:
- terminal runs (succeeded / failed / cancelled) with their sub-runs, events, tool intents and approvals;
- usage ledger rows; request idempotency records;
- the oldest audit rows. The audit chain stays verifiable: the last pruned (seq, hash) becomes the
  tenant's anchor, and the deletion itself is recorded in the audit log.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .config import Settings
from .governance import Audit, Principal
from .store import Store

log = logging.getLogger("uar.retention")
_LOCK = 0x5541_5232  # "UAR2"
SYSTEM = "system:retention"


class Retention:
    def __init__(self, settings: Settings, store: Store, audit: Audit):
        self.s = settings
        self.store = store
        self.audit = audit

    async def run_forever(self) -> None:
        cfg = self.s.retention
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("retention cycle failed")
            await asyncio.sleep(cfg.interval_s)

    async def run_once(self, now: datetime | None = None) -> dict[str, int]:
        cfg = self.s.retention
        now = now or datetime.now(timezone.utc)
        out: dict[str, int] = {}
        async with self.store.conn() as lock_conn:
            got = await (await lock_conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (_LOCK,))).fetchone()
            if not got["ok"]:
                return {"skipped": 1}
            try:
                if cfg.runs_days is not None:
                    out["runs"] = await self._runs(now - timedelta(days=cfg.runs_days))
                if cfg.usage_days is not None:
                    out["usage"] = await self.store.execute("DELETE FROM usage_ledger WHERE ts < %s",
                                                            now - timedelta(days=cfg.usage_days))
                if cfg.idempotency_days is not None:
                    out["idempotency"] = await self.store.execute("DELETE FROM idempotency WHERE created_at < %s",
                                                                  now - timedelta(days=cfg.idempotency_days))
                if cfg.audit_days is not None:
                    out["audit"] = await self._audit(now - timedelta(days=cfg.audit_days))
            finally:
                await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK,))
        if any(out.values()):
            log.info("retention pruned %s", out)
        return out

    async def _runs(self, cutoff: datetime) -> int:
        """Delete terminal top-level runs older than cutoff, with their whole sub-run tree."""
        async with self.store.tx() as c:
            cur = await c.execute(
                "WITH RECURSIVE old AS (SELECT run_id FROM runs WHERE parent_run_id IS NULL AND "
                "status IN ('succeeded','failed','cancelled') AND updated_at < %s), "
                "tree AS (SELECT run_id FROM old UNION ALL SELECT r.run_id FROM runs r JOIN tree ON r.parent_run_id = "
                "tree.run_id) DELETE FROM runs WHERE run_id IN (SELECT run_id FROM tree) RETURNING tenant, "
                "parent_run_id", (cutoff,))
            rows = await cur.fetchall()
        per_tenant: dict[str, int] = {}
        for r in rows:
            if r["parent_run_id"] is None:
                per_tenant[r["tenant"]] = per_tenant.get(r["tenant"], 0) + 1
        for tenant, n in per_tenant.items():
            await self.audit.record(Principal(tenant, SYSTEM, ()), "retention.delete", "runs", "succeeded",
                                    details={"runs": n, "before": cutoff.isoformat()}, required=False)
        return len(rows)

    async def _audit(self, cutoff: datetime) -> int:
        total = 0
        tenants = await self.store.fetchall("SELECT DISTINCT tenant FROM audit WHERE ts < %s", cutoff)
        for t in tenants:
            tenant = t["tenant"]
            async with self.store.tx() as c:
                await c.execute("SET LOCAL uar.audit_retention = 'on'")
                last = await (await c.execute(
                    "SELECT seq, hash FROM audit WHERE tenant=%s AND seq IS NOT NULL AND ts < %s "
                    "ORDER BY seq DESC LIMIT 1", (tenant, cutoff))).fetchone()
                if last is not None:
                    cur = await c.execute("DELETE FROM audit WHERE tenant=%s AND (seq <= %s OR (seq IS NULL AND ts < %s))",
                                          (tenant, last["seq"], cutoff))
                    await c.execute(
                        "INSERT INTO audit_anchors (tenant, seq, hash, pruned) VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT (tenant) DO UPDATE SET seq=EXCLUDED.seq, hash=EXCLUDED.hash, "
                        "pruned=audit_anchors.pruned + EXCLUDED.pruned, updated_at=now()",
                        (tenant, last["seq"], last["hash"], cur.rowcount))
                else:
                    cur = await c.execute("DELETE FROM audit WHERE tenant=%s AND seq IS NULL AND ts < %s",
                                          (tenant, cutoff))
                n = cur.rowcount
            total += n
            if n:
                await self.audit.record(Principal(tenant, SYSTEM, ()), "retention.delete", "audit", "succeeded",
                                        details={"rows": n, "before": cutoff.isoformat(),
                                                 "anchor_seq": last["seq"] if last else None}, required=False)
        return total
