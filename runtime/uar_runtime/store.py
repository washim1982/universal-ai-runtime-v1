"""PostgreSQL access: pool, migrations, and small typed helpers.

All queries that touch tenant data take the tenant explicitly. There is no API that
reads a run or agent by id alone, so a caller cannot reach another tenant's rows.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .errors import UARError

log = logging.getLogger("uar.store")
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
_MIGRATION_LOCK = 0x5541_5231  # "UAR1"


def selector_loop_factory():
    """psycopg's async driver needs a selector loop on Windows (MCP stdio has a Popen fallback)."""
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop
    return None


def jsonb(v: Any) -> Jsonb | None:
    return None if v is None else Jsonb(v)


class Store:
    def __init__(self, url: str, min_size: int = 1, max_size: int = 10):
        self.url = url
        self.pool = AsyncConnectionPool(url, min_size=min_size, max_size=max_size, open=False,
                                        kwargs={"row_factory": dict_row, "autocommit": True})

    async def open(self) -> None:
        try:
            await self.pool.open(wait=True, timeout=15)
        except Exception as e:  # pragma: no cover - depends on environment
            raise UARError("unavailable", "database unavailable", details={"error": type(e).__name__}) from e

    async def close(self) -> None:
        await self.pool.close()

    @asynccontextmanager
    async def conn(self) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self.pool.connection() as c:
            yield c

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self.pool.connection() as c:
            async with c.transaction():
                yield c

    async def fetchone(self, sql: str, *args: Any) -> dict | None:
        async with self.conn() as c:
            cur = await c.execute(sql, args)
            return await cur.fetchone()

    async def fetchall(self, sql: str, *args: Any) -> list[dict]:
        async with self.conn() as c:
            cur = await c.execute(sql, args)
            return await cur.fetchall()

    async def execute(self, sql: str, *args: Any) -> int:
        async with self.conn() as c:
            cur = await c.execute(sql, args)
            return cur.rowcount

    async def ping(self) -> bool:
        try:
            await self.fetchone("SELECT 1 AS ok")
            return True
        except Exception:
            return False

    async def migrate(self) -> list[str]:
        """Apply pending migrations under an advisory lock (safe with several replicas)."""
        applied: list[str] = []
        async with self.pool.connection() as c:
            await c.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
            try:
                await c.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                                "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
                done = {r["version"] for r in await (await c.execute("SELECT version FROM schema_migrations")).fetchall()}
                for f in sorted(MIGRATIONS.glob("*.sql")):
                    if f.stem in done:
                        continue
                    async with c.transaction():
                        await c.execute(f.read_text(encoding="utf-8"))
                        await c.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (f.stem,))
                    applied.append(f.stem)
                    log.info("applied migration %s", f.stem)
            finally:
                await c.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))
        return applied


def dumps(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"), sort_keys=True, default=str)
