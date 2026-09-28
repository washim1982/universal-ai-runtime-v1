"""uar command line.

    uar serve         HTTP + gRPC gateway (with an embedded worker unless worker.embedded=false)
    uar worker        engine worker only (distributed profile)
    uar migrate       apply database migrations
    uar check-config  validate configuration and exit
    uar new-key       print a new API key id, key and sha256 for configuration
    uar register-app  register an application for the token service (prints client id + secret once)
"""
from __future__ import annotations

import argparse
import asyncio
import secrets
import signal
import sys

import uvicorn

from .config import load_settings
from .governance import hash_key
from .observability import setup_logging, setup_tracing
from .store import Store, selector_loop_factory


def _run(coro) -> None:
    factory = selector_loop_factory()
    if factory:
        asyncio.run(coro, loop_factory=factory)
    else:
        asyncio.run(coro)


async def serve(config: str | None) -> None:
    from .gateway.grpc_server import start_grpc
    from .gateway.http import create_app
    from .service import RuntimeService

    s = load_settings(config)
    setup_logging(s.observability.log_level, s.observability.log_json)
    setup_tracing(s.observability.service_name, s.observability.otlp_endpoint)
    svc = RuntimeService(s, Store(s.database_url))
    await svc.start()
    grpc_server = await start_grpc(svc, s.server.host, s.server.grpc_port) if s.server.grpc_enabled else None
    server = uvicorn.Server(uvicorn.Config(create_app(svc), host=s.server.host, port=s.server.http_port,
                                           log_config=None, access_log=False, timeout_graceful_shutdown=10))
    try:
        await server.serve()
    finally:
        if grpc_server:
            await grpc_server.stop(5)
        await svc.stop()


async def worker(config: str | None) -> None:
    from .service import RuntimeService

    s = load_settings(config)
    setup_logging(s.observability.log_level, s.observability.log_json)
    setup_tracing(s.observability.service_name, s.observability.otlp_endpoint)
    svc = RuntimeService(s, Store(s.database_url))
    await svc.start(worker=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop.wait()
    finally:
        await svc.stop()


async def migrate(config: str | None) -> None:
    s = load_settings(config)
    store = Store(s.database_url)
    await store.open()
    applied = await store.migrate()
    await store.close()
    print("applied: " + (", ".join(applied) or "nothing (up to date)"))


async def register_app(config: str | None, tenant: str, name: str, roles: list[str], ttl: int) -> None:
    """Bootstrap: register an application directly in the database (for runtimes with
    sts.require_tokens, where no API key exists to call the admin API with)."""
    from .governance import Principal
    from .service import RuntimeService

    s = load_settings(config)
    if tenant not in {t.id for t in s.auth.tenants}:
        sys.exit(f"unknown tenant {tenant}")
    svc = RuntimeService(s, Store(s.database_url))
    await svc.store.open()
    await svc.store.migrate()
    await svc.sts.start()
    try:
        p = Principal(tenant, "cli:register-app", ("admin",), "", svc.auth.permissions(("admin",)))
        out = await svc.sts.register_app(p, {"name": name, "roles": roles, "token_ttl_s": ttl}, "", svc.public_url())
    finally:
        await svc.sts.stop()
        await svc.store.close()
    print(f"client_id:     {out['client_id']}\n"
          f"client_secret: {out['client_secret']}\n"
          f"token_url:     {out['token_url']}\n"
          "The secret is shown only now; store it in your secret manager.")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="uar")
    ap.add_argument("command", choices=["serve", "worker", "migrate", "check-config", "new-key", "register-app"])
    ap.add_argument("--config", default=None, help="path to uar.yaml (default: $UAR_CONFIG or config/uar.yaml)")
    ap.add_argument("--tenant", help="register-app: tenant id")
    ap.add_argument("--name", help="register-app: application name")
    ap.add_argument("--roles", default="", help="register-app: comma-separated roles, e.g. admin or developer,viewer")
    ap.add_argument("--token-ttl", type=int, default=0, help="register-app: access token lifetime in seconds")
    a = ap.parse_args(argv)
    if a.command == "register-app":
        if not (a.tenant and a.name and a.roles):
            ap.error("register-app needs --tenant, --name and --roles")
        _run(register_app(a.config, a.tenant, a.name, [r.strip() for r in a.roles.split(",") if r.strip()], a.token_ttl))
        return
    if a.command == "new-key":
        kid = secrets.token_hex(4)
        key = f"uar_{kid}_{secrets.token_urlsafe(24)}"
        print(f"id: {kid}\nkey: {key}\nsha256: {hash_key(key)}")
        return
    if a.command == "check-config":
        s = load_settings(a.config)
        print(f"ok: profile={s.profile} providers={len(s.providers)} mcp_servers={len(s.mcp_servers)}")
        return
    try:
        _run({"serve": serve, "worker": worker, "migrate": migrate}[a.command](a.config))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
