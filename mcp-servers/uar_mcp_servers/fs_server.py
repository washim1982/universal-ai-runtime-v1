"""Filesystem MCP server confined to one root.

Environment:
  UAR_FS_ROOT            directory exposed (required)
  UAR_FS_WRITE_DIRS      comma-separated top-level subdirectories where write_text may create files
  UAR_FS_MAX_READ_BYTES  read cap (default 1 MiB)

Paths are relative POSIX-style paths. Absolute paths, drive letters, "..", NUL, ":" (Windows
alternate data streams) and any symlink or junction component are rejected. Writes create new
files only (never overwrite).
"""
from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .common import serve

ROOT = Path(os.environ.get("UAR_FS_ROOT", ".")).resolve(strict=True)
WRITE_DIRS = {d.strip().strip("/") for d in os.environ.get("UAR_FS_WRITE_DIRS", "").split(",") if d.strip()}
MAX_READ = int(os.environ.get("UAR_FS_MAX_READ_BYTES", str(1024 * 1024)))
MAX_WRITE = 1024 * 1024

server = MCPServer("uar-fs", version="1.0.0",
                   instructions="Read and list files under a fixed root; create new files in allowed directories.")


def _is_link(p: Path) -> bool:
    return p.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(p))


def safe_path(rel: str, *, for_write: bool = False) -> Path:
    if not isinstance(rel, str) or not rel.strip() or "\x00" in rel or ":" in rel:
        raise ToolError("invalid path")
    norm = rel.replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise ToolError("path must be relative to the root")
    parts = [p for p in PurePosixPath(norm).parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ToolError("path must not contain '..'")
    cur = ROOT
    for part in parts:
        cur = cur / part
        if _is_link(cur):
            raise ToolError("links are not allowed")
    if not cur.resolve().is_relative_to(ROOT):
        raise ToolError("path escapes the root")
    if for_write:
        if not parts or parts[0] not in WRITE_DIRS or len(parts) < 2:
            raise ToolError(f"writes are only allowed inside: {sorted(WRITE_DIRS) or 'nowhere'}")
    return cur


@server.tool(description="Read a UTF-8 text file (relative path under the root).", structured_output=True)
def read_text(path: str) -> dict[str, Any]:
    p = safe_path(path)
    if not p.is_file():
        raise ToolError("not a file")
    data = p.read_bytes()[: MAX_READ + 1]
    truncated = len(data) > MAX_READ
    return {"path": path, "content": data[:MAX_READ].decode("utf-8", errors="replace"), "bytes": p.stat().st_size,
            "truncated": truncated}


@server.tool(description="List a directory (relative path under the root, default '.').", structured_output=True)
def list_dir(path: str = ".") -> dict[str, Any]:
    p = ROOT if path in (".", "", "./") else safe_path(path)
    if not p.is_dir():
        raise ToolError("not a directory")
    entries = []
    for child in sorted(p.iterdir())[:500]:
        if _is_link(child):
            continue
        entries.append({"name": child.name, "type": "dir" if child.is_dir() else "file",
                        "size": child.stat().st_size if child.is_file() else None})
    return {"path": path, "entries": entries}


@server.tool(description="Create a NEW UTF-8 text file inside an allowed write directory. Never overwrites.", structured_output=True)
def write_text(path: str, content: str) -> dict[str, Any]:
    p = safe_path(path, for_write=True)
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE:
        raise ToolError("content too large")
    parent = p.parent
    if not parent.exists():
        parent.mkdir(parents=True)
        safe_path(str(parent.relative_to(ROOT)).replace("\\", "/"), for_write=False)
    try:
        with open(p, "xb") as f:  # exclusive create: fails if the file exists
            f.write(data)
    except FileExistsError:
        raise ToolError("file already exists; writes never overwrite") from None
    return {"path": path, "bytes": len(data), "created": True}


if __name__ == "__main__":
    serve(server)
