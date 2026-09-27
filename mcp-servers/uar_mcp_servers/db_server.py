"""Read-only SQL MCP server over a SQLite database.

Environment:
  UAR_DB_PATH        database file (opened read-only, mode=ro)
  UAR_DB_MAX_ROWS    row cap per query (default 500)
  UAR_DB_TIMEOUT_S   per-query time limit (default 5)

Only single SELECT statements run: an SQLite authorizer denies every action except reads,
selects and functions, so writes, PRAGMA, ATTACH and schema changes fail even if the
file were writable. Parameters are bound, never interpolated.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .common import serve

DB = Path(os.environ.get("UAR_DB_PATH", "sales.db")).resolve()
MAX_ROWS = int(os.environ.get("UAR_DB_MAX_ROWS", "500"))
TIMEOUT_S = float(os.environ.get("UAR_DB_TIMEOUT_S", "5"))

server = MCPServer("uar-db", version="1.0.0", instructions="Read-only SQL (SQLite dialect) over a fixed database.")

_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}


def _authorizer(action, *_):
    return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, check_same_thread=False)
    conn.set_authorizer(_authorizer)
    return conn


@server.tool(description="Run one read-only SELECT statement with optional positional parameters (?).", structured_output=True)
def query(sql: str, params: list[str | int | float | None] | None = None) -> dict[str, Any]:
    if len(sql) > 10_000:
        raise ToolError("query too long")
    conn = _connect()
    deadline = time.monotonic() + TIMEOUT_S
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        cur = conn.execute(sql, list(params or []))
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(MAX_ROWS + 1)
    except sqlite3.DatabaseError as e:
        msg = str(e)
        if "interrupted" in msg:
            raise ToolError("query exceeded the time limit") from None
        if "not authorized" in msg or "prohibited" in msg:
            raise ToolError("only read-only SELECT statements are allowed") from None
        raise ToolError(f"query failed: {msg}") from None
    finally:
        conn.close()
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    return {"columns": cols, "rows": [list(r) for r in rows], "row_count": len(rows), "truncated": truncated}


@server.tool(description="List tables and their columns.", structured_output=True)
def list_tables() -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {"tables": [{"name": t, "columns": [c[1] for c in conn.execute(f'PRAGMA table_info("{t}")')]}
                           for t in tables]}
    finally:
        conn.close()


if __name__ == "__main__":
    serve(server)
