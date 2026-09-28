"""Test harness: throwaway PostgreSQL databases, temporary workspaces, generated keys,
deterministic fake providers, and real HTTP + gRPC servers on random ports."""
from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
import uvicorn

from uar_runtime.config import Settings
from uar_runtime.governance import hash_key
from uar_runtime.service import RuntimeService
from uar_runtime.store import Store

ROOT = Path(__file__).resolve().parents[2]
PG_ADMIN = os.environ.get("UAR_TEST_PG", "postgresql://uar:uar-dev@127.0.0.1:55440/postgres")


def pytest_asyncio_loop_factories(config, item):
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


def _key() -> tuple[str, str]:
    kid = secrets.token_hex(4)
    key = f"uar_{kid}_{secrets.token_urlsafe(24)}"
    return kid, key


@dataclass
class Keys:
    dev: str
    admin: str
    viewer: str
    ops: str
    other_tenant: str
    cloud_dev: str
    approver: str
    finance: str
    auditor: str
    globex_admin: str


def make_keys() -> tuple[Keys, list[dict]]:
    names = ["dev", "admin", "viewer", "ops", "other_tenant", "cloud_dev", "approver", "finance", "auditor",
             "globex_admin"]
    ks = {n: _key() for n in names}
    tenants = [
        {"id": "acme", "allow_cloud": False, "quotas": {"requests_per_minute": 100000, "concurrent_requests": 64},
         "api_keys": [
             {"id": ks["dev"][0], "sha256": hash_key(ks["dev"][1]), "subject": "dev@acme", "roles": ["developer"]},
             {"id": ks["admin"][0], "sha256": hash_key(ks["admin"][1]), "subject": "admin@acme", "roles": ["admin"]},
             {"id": ks["viewer"][0], "sha256": hash_key(ks["viewer"][1]), "subject": "viewer@acme", "roles": ["viewer"]},
             {"id": ks["ops"][0], "sha256": hash_key(ks["ops"][1]), "subject": "ops@acme", "roles": ["operator"]},
             {"id": ks["approver"][0], "sha256": hash_key(ks["approver"][1]), "subject": "approver@acme",
              "roles": ["approver"]},
             {"id": ks["finance"][0], "sha256": hash_key(ks["finance"][1]), "subject": "cfo@acme",
              "roles": ["approver", "finance"]},
             {"id": ks["auditor"][0], "sha256": hash_key(ks["auditor"][1]), "subject": "auditor@acme",
              "roles": ["auditor"]},
         ]},
        {"id": "globex", "quotas": {"requests_per_minute": 100000, "concurrent_requests": 64},
         "api_keys": [{"id": ks["other_tenant"][0], "sha256": hash_key(ks["other_tenant"][1]),
                       "subject": "dev@globex", "roles": ["developer"]},
                      {"id": ks["globex_admin"][0], "sha256": hash_key(ks["globex_admin"][1]),
                       "subject": "admin@globex", "roles": ["admin"]}]},
        {"id": "cloudco", "allow_cloud": True, "allow_cloud_fallback": True,
         "quotas": {"requests_per_minute": 100000, "concurrent_requests": 64, "tokens_per_day": 1_000_000},
         "api_keys": [{"id": ks["cloud_dev"][0], "sha256": hash_key(ks["cloud_dev"][1]), "subject": "dev@cloudco",
                       "roles": ["developer", "cloud_user"]}]},
    ]
    return Keys(*(ks[n][1] for n in names)), tenants


def make_workspace(base: Path) -> Path:
    ws = base / "workspace"
    (ws / "reports").mkdir(parents=True)
    (ws / "docs").mkdir()
    (ws / "docs" / "faq.md").write_text("# FAQ\nReturns within 30 days.\n", encoding="utf-8", newline="\n")
    (ws / "secret-outside.txt").write_text("root file", encoding="utf-8")
    shutil.copyfile(ROOT / "examples" / "workspace" / "docs" / "product-faq.md", ws / "docs" / "product-faq.md")
    shutil.copyfile(ROOT / "examples" / "fixtures" / "sales.db", base / "sales.db")
    return ws


def settings_dict(db_url: str, tenants: list[dict], ws: Path, base: Path, **over) -> dict:
    d = {
        "profile": "standard",
        "database_url": db_url,
        "server": {"host": "127.0.0.1", "http_port": 0, "grpc_port": 0},
        "worker": {"embedded": True, "concurrency": 4, "lease_s": 3, "heartbeat_s": 1, "poll_s": 0.1},
        "observability": {"log_json": False, "log_level": "WARNING"},
        "auth": {"tenants": tenants,
                 "roles": {"viewer": ["models:list", "tools:list", "agents:read", "runs:read"],
                           "developer": ["models:list", "tools:list", "tools:execute", "inference:local",
                                         "inference:enterprise", "agents:register", "agents:read", "runs:start",
                                         "runs:read", "runs:cancel", "dryrun", "guardrails:check"],
                           "cloud_user": ["inference:cloud"],
                           "operator": ["models:list", "tools:list", "agents:read", "runs:read", "runs:cancel",
                                        "runs:resolve", "dryrun"],
                           "approver": ["runs:read", "approvals:read", "approvals:decide"],
                           "finance": ["approvals:read"],
                           "auditor": ["runs:read", "approvals:read", "audit:read"],
                           "admin": ["admin"]},
                 "jwt": {"hs256_secret_env": "UAR_TEST_JWT_SECRET"}},
        "providers": [
            {"id": "fake", "type": "fake", "model_class": "local", "models": ["echo", "echo-large"],
             "max_concurrency": 8},
            {"id": "fake_b", "type": "fake", "model_class": "local", "models": ["echo", "other"], "max_concurrency": 8},
            {"id": "fakecloud", "type": "fake", "model_class": "cloud", "models": ["big"], "max_concurrency": 8},
        ],
        "router": {"default_class": "local",
                   "classes": {"local": ["fake", "fake_b"], "cloud": ["fakecloud"]},
                   "aliases": {"local:default": "fake/echo", "cloud:default": "fakecloud/big"},
                   "rules": [{"when": {"data_class": "confidential"}, "deny": ["cloud"]}],
                   "fallback": [{"from": "local:fake/echo", "to": "local:fake_b/echo"},
                                {"from": "local:fake/echo-large", "to": "cloud:default"}],
                   "circuit_breaker": {"failure_threshold": 3, "window_s": 30, "cooldown_s": 1}},
        "pricing": {"version": "test-1", "currency": "USD",
                    "models": {"fake/*": {"input_per_mtok": "0", "output_per_mtok": "0"},
                               "fakecloud/big": {"input_per_mtok": "3", "output_per_mtok": "15"}}},
        "mcp_servers": [
            {"id": "fs", "command": "python", "args": ["-m", "uar_mcp_servers.fs_server"],
             "env": {"UAR_FS_ROOT": str(ws), "UAR_FS_WRITE_DIRS": "reports"},
             "tools": {"write_text": {"side_effect": "write"}}, "timeout_s": 20},
            {"id": "db", "command": "python", "args": ["-m", "uar_mcp_servers.db_server"],
             "env": {"UAR_DB_PATH": str(base / "sales.db")}, "timeout_s": 20},
        ],
        "tool_policies": [
            {"tool": "fs.write_text", "roles": ["developer", "admin"], "args": {"path": {"prefix": ["reports/"]}}},
            {"tool": "fs.*", "roles": ["developer", "admin", "operator"]},
            {"tool": "db.*", "roles": ["developer", "admin"]},
        ],
        "limits": {"max_steps": 50, "max_loop_iterations": 20, "max_tokens": 50000, "max_tool_calls": 50,
                   "timeout_s": 600, "max_depth": 2},
        "agents_dir": str(ROOT / "examples" / "agents"),
    }
    for k, v in over.items():
        d[k] = v
    return d


def create_db(name: str) -> str:
    with psycopg.connect(PG_ADMIN, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}"')
        c.execute(f'CREATE DATABASE "{name}"')
    return PG_ADMIN.rsplit("/", 1)[0] + "/" + name


def drop_db(name: str) -> None:
    with psycopg.connect(PG_ADMIN, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@dataclass
class Env:
    svc: RuntimeService
    keys: Keys
    http: str
    grpc: str
    ws: Path
    base: Path
    db_url: str


async def start_env(tmp: Path, name: str, *, worker: bool = True, serve: bool = True, customize=None,
                    **over) -> tuple[Env, list]:
    os.environ.setdefault("UAR_TEST_JWT_SECRET", secrets.token_urlsafe(32))
    keys, tenants = make_keys()
    ws = make_workspace(tmp)
    db_url = create_db(name)
    d = settings_dict(db_url, tenants, ws, tmp, **over)
    if customize is not None:
        customize(d)
    s = Settings.model_validate(d)
    s.worker.embedded = worker
    svc = RuntimeService(s, Store(db_url, max_size=20))
    await svc.start()
    cleanup: list = []
    http = grpc_target = ""
    if serve:
        from uar_runtime.gateway.grpc_server import start_grpc
        from uar_runtime.gateway.http import create_app
        server = uvicorn.Server(uvicorn.Config(create_app(svc), host="127.0.0.1", port=0, log_config=None,
                                               access_log=False, lifespan="off"))
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        http = f"http://127.0.0.1:{port}"
        g = await start_grpc(svc, "127.0.0.1", 0)
        grpc_target = f"127.0.0.1:{g.bound_port}"
        cleanup += [(server, task), g]
    return Env(svc, keys, http, grpc_target, ws, tmp, db_url), cleanup


async def stop_env(env: Env, cleanup: list, name: str) -> None:
    for item in cleanup:
        if isinstance(item, tuple):
            server, task = item
            server.should_exit = True
            await asyncio.wait_for(task, 10)
        else:
            await item.stop(1)
    await env.svc.stop()
    drop_db(name)


@pytest.fixture(scope="session")
async def env(tmp_path_factory):
    name = f"uar_test_{os.getpid()}"
    e, cleanup = await start_env(tmp_path_factory.mktemp("env"), name)
    yield e
    await stop_env(e, cleanup, name)


@pytest.fixture(scope="session")
async def manual(tmp_path_factory):
    """A runtime with no embedded worker: tests drive claim/execute directly (crash simulation)."""
    name = f"uar_test_manual_{os.getpid()}"
    e, cleanup = await start_env(tmp_path_factory.mktemp("manual"), name, worker=False, serve=False)
    yield e
    await stop_env(e, cleanup, name)


def headers(key: str) -> dict:
    return {"X-API-Key": key}
