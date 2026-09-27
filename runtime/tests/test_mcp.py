"""M3 gates: governed MCP execution against real MCP server processes."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import httpx
import pytest
from pathlib import Path

from conftest import headers
from uar_runtime.errors import UARError
from uar_runtime.mcp.orchestrator import check_url_egress


async def call(env, tool, args, key=None):
    async with httpx.AsyncClient(base_url=env.http, headers=headers(key or env.keys.dev), timeout=60) as c:
        return await c.post("/api/v1/tool/execute", json={"tool": tool, "args": args})


async def test_read_and_list(env):
    r = await call(env, "fs.read_text", {"path": "docs/faq.md"})
    body = r.json()
    assert r.status_code == 200 and body["structured"]["content"].startswith("# FAQ")
    assert body["untrusted"] is True and body["is_error"] is False
    r = await call(env, "fs.list_dir", {"path": "docs"})
    assert [e["name"] for e in r.json()["structured"]["entries"]] == ["faq.md", "product-faq.md"]


@pytest.mark.parametrize("path", ["../outside.txt", "docs/../../x", "/etc/passwd", "C:/Windows/win.ini",
                                  "C:\\Windows\\win.ini", "docs/faq.md:stream", "docs\\..\\..\\x", "docs/\x00x"])
async def test_traversal_rejected(env, path):
    r = await call(env, "fs.read_text", {"path": path})
    assert r.status_code == 200 and r.json()["is_error"] is True, path


async def test_link_escape_rejected(env, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    made = []
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(outside), str(env.ws / "docs" / "junction"))
        made.append("junction")
    try:
        os.symlink(outside / "secret.txt", env.ws / "docs" / "link.txt")
        made.append("link.txt")
    except OSError:
        pass  # symlinks need extra privileges on Windows; the junction covers the case
    assert made, "could not create any link to test"
    try:
        for name in made:
            target = f"docs/{name}/secret.txt" if name == "junction" else f"docs/{name}"
            r = await call(env, "fs.read_text", {"path": target})
            assert r.json()["is_error"] is True and "secret" not in str(r.json().get("structured")), name
    finally:
        for name in made:
            p = env.ws / "docs" / name
            os.rmdir(p) if name == "junction" else p.unlink()


async def test_write_policy_and_exclusive_create(env):
    r = await call(env, "fs.write_text", {"path": "docs/new.md", "content": "x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "policy_denied"
    r = await call(env, "fs.write_text", {"path": "reports/r1.md", "content": "hello"})
    assert r.status_code == 200 and r.json()["is_error"] is False
    assert (env.ws / "reports" / "r1.md").read_text(encoding="utf-8") == "hello"
    r = await call(env, "fs.write_text", {"path": "reports/r1.md", "content": "overwrite"})
    assert r.json()["is_error"] is True
    assert (env.ws / "reports" / "r1.md").read_text(encoding="utf-8") == "hello"
    r = await call(env, "fs.write_text", {"path": "reports/x.md", "content": "x"}, key=env.keys.ops)
    assert r.status_code == 403, "operators may read but not write"
    intents = await env.svc.store.fetchall("SELECT status FROM tool_intents WHERE tool='fs.write_text' "
                                           "ORDER BY created_at")
    assert [i["status"] for i in intents][-2:] == ["completed", "failed"]


async def test_argument_schema_validation(env):
    r = await call(env, "fs.read_text", {"path": 42})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_argument"
    r = await call(env, "fs.read_text", {})
    assert r.status_code == 400
    r = await call(env, "no.such_tool", {})
    assert r.status_code == 404


async def test_database_read_only(env):
    r = await call(env, "db.query", {"sql": "SELECT region, SUM(units) AS u FROM sales WHERE quarter = ? "
                                            "GROUP BY region ORDER BY region", "params": ["2026-Q1"]})
    d = r.json()["structured"]
    assert d["columns"] == ["region", "u"] and len(d["rows"]) == 4
    for sql in ["DELETE FROM sales", "UPDATE sales SET units = 0", "DROP TABLE sales",
                "ATTACH DATABASE 'x.db' AS x", "PRAGMA writable_schema = 1",
                "SELECT 1; DELETE FROM sales", "CREATE TABLE t(x)"]:
        r = await call(env, "db.query", {"sql": sql})
        assert r.json()["is_error"] is True, sql
    r = await call(env, "db.query", {"sql": "SELECT COUNT(*) AS n FROM sales"})
    assert r.json()["structured"]["rows"] == [[32]]


async def test_server_crash_recovers(env):
    sess = env.svc.orch.sessions["db"]
    # Kill the server process out from under the session.
    await sess.reset()
    r = await call(env, "db.query", {"sql": "SELECT 1 AS one"})
    assert r.status_code == 200 and r.json()["structured"]["rows"] == [[1]]


async def test_tool_output_is_capped(env):
    big = env.ws / "docs" / "big.txt"
    big.write_text("y" * 400_000, encoding="utf-8")
    try:
        r = await call(env, "fs.read_text", {"path": "docs/big.txt"})
        assert r.json()["truncated"] is True
    finally:
        big.unlink()


async def test_idempotent_tool_execute(env):
    async with httpx.AsyncClient(base_url=env.http, headers={**headers(env.keys.dev), "Idempotency-Key": "k-1"}) as c:
        body = {"tool": "fs.write_text", "args": {"path": "reports/idem.md", "content": "once"}}
        r1 = await c.post("/api/v1/tool/execute", json=body)
        r2 = await c.post("/api/v1/tool/execute", json=body)
        assert r1.json()["is_error"] is False and r2.json() == r1.json()
        body["args"]["content"] = "different"
        r3 = await c.post("/api/v1/tool/execute", json=body)
        assert r3.status_code == 409 and r3.json()["error"]["code"] == "idempotency_mismatch"


def test_ssrf_guard():
    for url in ["http://169.254.169.254/latest", "http://10.0.0.5/mcp", "file:///etc/passwd", "gopher://x",
                "http://[::1]:9/"]:
        with pytest.raises(UARError) as ei:
            check_url_egress(url, [])
        assert ei.value.code == "egress_denied", url
    check_url_egress("http://127.0.0.1:8765/mcp", ["127.0.0.0/8"])  # explicitly allowed


@pytest.mark.docker
async def test_container_sandbox(tmp_path):
    """The fs server inside a locked-down container: no network, read-only rootfs, dropped caps."""
    if not shutil.which("docker"):
        pytest.skip("docker not installed")
    img = subprocess.run(["docker", "image", "inspect", "uar-runtime:0.9.0"], capture_output=True)
    if img.returncode != 0:
        pytest.skip("build the image first: docker build -f deploy/docker/Dockerfile -t uar-runtime:0.9.0 .")
    from uar_runtime.config import McpServerCfg, MountCfg, Settings
    from uar_runtime.mcp.orchestrator import ServerSession
    ws = tmp_path / "ws"
    (ws / "reports").mkdir(parents=True)
    (ws / "a.txt").write_text("inside", encoding="utf-8")
    # The container runs as uid 10001; on Linux the bind-mounted temp dir belongs to the CI user,
    # so let the sandbox user write into this throwaway workspace (Docker Desktop ignores this).
    for d in (ws, ws / "reports"):
        os.chmod(d, 0o777)
    cfg = McpServerCfg(id="cfs", sandbox="container", image="uar-runtime:0.9.0",
                       command="python", args=["-m", "uar_mcp_servers.fs_server"],
                       env={"UAR_FS_ROOT": "/workspace", "UAR_FS_WRITE_DIRS": "reports"},
                       mounts=[MountCfg(src=str(ws), dst="/workspace", readonly=False)], timeout_s=60)
    settings = Settings.model_construct(egress=__import__("uar_runtime.config", fromlist=["EgressCfg"]).EgressCfg())
    sess = ServerSession(cfg, settings)
    try:
        await sess.ensure()
        assert set(sess.tools) == {"cfs.read_text", "cfs.list_dir", "cfs.write_text"}
        res = await sess.call("read_text", {"path": "a.txt"}, 30)
        assert res.structured_content["content"] == "inside"
        res = await sess.call("write_text", {"path": "reports/c.md", "content": "from container"}, 30)
        assert not res.is_error and (ws / "reports" / "c.md").read_text() == "from container"
        ids = subprocess.run(["docker", "ps", "-q", "--filter", "label=uar.mcp=1", "--filter", "name=uar-mcp-cfs"],
                             capture_output=True, text=True).stdout.split()
        assert len(ids) == 1
        hc = json.loads(subprocess.run(["docker", "inspect", ids[0]], capture_output=True, text=True).stdout)[0]
        assert hc["HostConfig"]["NetworkMode"] == "none" and hc["HostConfig"]["ReadonlyRootfs"] is True
        assert hc["HostConfig"]["CapDrop"] == ["ALL"] and "no-new-privileges" in hc["HostConfig"]["SecurityOpt"]
        assert hc["Config"]["User"] == "10001:10001" and hc["HostConfig"]["Memory"] == 256 * 1024 * 1024
    finally:
        await sess.reset()


async def test_streamable_http_transport(tmp_path):
    """Remote MCP over Streamable HTTP (the Kubernetes profile), including the egress guard."""
    import socket as _s
    import time as _t
    from uar_runtime.config import EgressCfg, McpServerCfg, Settings
    from uar_runtime.mcp.orchestrator import ServerSession
    ws = tmp_path / "ws"
    (ws / "reports").mkdir(parents=True)
    (ws / "hello.txt").write_text("over http", encoding="utf-8")
    sock = _s.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    root = Path(__file__).resolve().parents[2]
    proc = subprocess.Popen([sys.executable, "-m", "uar_mcp_servers.fs_server", "--http", "--port", str(port)],
                            env={**os.environ, "UAR_FS_ROOT": str(ws), "PYTHONPATH": str(root / "mcp-servers")},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                _s.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                _t.sleep(0.1)
        cfg = McpServerCfg(id="rfs", transport="http", url=f"http://127.0.0.1:{port}/mcp", timeout_s=20)
        denied = ServerSession(cfg, Settings.model_construct(egress=EgressCfg(allow_private=[])))
        with pytest.raises(UARError) as ei:
            await denied.ensure()
        assert ei.value.code == "egress_denied"
        sess = ServerSession(cfg, Settings.model_construct(egress=EgressCfg(allow_private=["127.0.0.0/8"])))
        await sess.ensure()
        try:
            assert set(sess.tools) == {"rfs.read_text", "rfs.list_dir", "rfs.write_text"}
            res = await sess.call("read_text", {"path": "hello.txt"}, 20)
            assert res.structured_content["content"] == "over http"
        finally:
            await sess.reset()
    finally:
        proc.terminate()
        proc.wait(10)
