"""Plugin runtime: immutable versions, out-of-process instances, activation, rollback and pinning.

A plugin version runs as its own process, a locked-down container, or an external endpoint, and
speaks uar.plugin.v1 over gRPC. Nothing third-party is imported into the runtime process.

Activation = integrity check (artifact or image digest) -> start -> Describe (must match the
manifest and request no capability beyond what the administrator declared) -> Init (config +
declared secrets only) -> Health. Only then does the version become active, and it affects new
runs only: every run records the versions active when it started (`runs.plugins`) and keeps using
them, while older versions stay available for runs that pinned them. Rollback re-activates the
previous version for new runs.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import grpc
import jsonschema
from google.protobuf import json_format, struct_pb2

from uarpb.plugin.v1 import plugin_pb2 as ppb
from uarpb.plugin.v1 import plugin_pb2_grpc as ppbg

from ..errors import UARError, not_found
from ..governance import Audit, Principal
from ..store import Store, jsonb

log = logging.getLogger("uar.plugins")
REPO = Path(__file__).resolve().parents[3]
SCHEMA = REPO / "contracts" / "schemas" / "plugin.schema.json"

# Plugin versions pinned by the run being executed ({plugin_id: version}); empty outside runs.
current_pins: contextvars.ContextVar[dict] = contextvars.ContextVar("uar_plugin_pins", default={})


@lru_cache(maxsize=1)
def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))


def manifest_digest(manifest: dict) -> str:
    canon = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canon.encode()).hexdigest()


def validate_manifest(manifest: dict) -> None:
    errors = sorted(_validator().iter_errors(manifest), key=lambda e: list(e.absolute_path))
    if errors:
        e = errors[0]
        where = "/".join(str(p) for p in e.absolute_path) or "(root)"
        raise UARError("invalid_argument", f"invalid plugin manifest at {where}: {e.message}")
    rt = manifest["spec"]["runtime"]
    if rt["mode"] == "none":
        if manifest["spec"]["type"] != "agent" or not manifest["spec"].get("graph"):
            raise UARError("invalid_argument", "runtime.mode none is only for declarative agent plugins (spec.graph)")
        return
    need = {"process": "command", "container": "image", "external": "endpoint"}[rt["mode"]]
    if not rt.get(need):
        raise UARError("invalid_argument", f"runtime.mode {rt['mode']} needs runtime.{need}")


def declared_capabilities(manifest: dict) -> set[str]:
    caps = manifest["spec"].get("capabilities") or {}
    return {f"network:{h}" for h in caps.get("network", [])} | {f"secret:{s}" for s in caps.get("secrets", [])}


def to_struct(d: dict | None) -> struct_pb2.Struct:
    s = struct_pb2.Struct()
    if d:
        json_format.ParseDict(json.loads(json.dumps(d, default=str)), s)
    return s


def from_struct(s: struct_pb2.Struct) -> dict:
    def norm(v):
        if isinstance(v, float) and v.is_integer():
            return int(v)
        if isinstance(v, dict):
            return {k: norm(x) for k, x in v.items()}
        if isinstance(v, list):
            return [norm(x) for x in v]
        return v
    return norm(json_format.MessageToDict(s))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------------------------------ instance

@dataclass
class Instance:
    plugin_id: str
    version: str
    manifest: dict
    address: str = ""
    proc: subprocess.Popen | None = None
    container: str | None = None
    channel: grpc.aio.Channel | None = None
    stub: ppbg.PluginStub | None = None
    descriptor: ppb.PluginDescriptor | None = None
    sem: asyncio.Semaphore | None = None
    failures: list[float] = field(default_factory=list)
    open_until: float = 0.0

    @property
    def timeout_s(self) -> float:
        return float((self.manifest["spec"].get("limits") or {}).get("timeout_s", 30))

    def breaker_open(self) -> bool:
        return time.monotonic() < self.open_until

    def record(self, ok: bool) -> None:
        now = time.monotonic()
        if ok:
            self.failures.clear()
            return
        self.failures = [t for t in self.failures if now - t < 30] + [now]
        if len(self.failures) >= 3:
            self.open_until = now + 30
            log.warning("plugin %s@%s circuit opened", self.plugin_id, self.version)

    async def start(self) -> None:
        spec = self.manifest["spec"]
        rt = spec["runtime"]
        limits = spec.get("limits") or {}
        self.sem = asyncio.Semaphore(int(limits.get("concurrency", 8)))
        if rt["mode"] == "none":  # declarative package: nothing to run
            md = self.manifest["metadata"]
            self.descriptor = ppb.PluginDescriptor(id=md["id"], version=md["version"], kind=spec["type"],
                                                   api="uar.plugin.v1")
            return
        if rt["mode"] == "external":
            self.address = rt["endpoint"]
        elif rt["mode"] == "process":
            port = _free_port()
            self.address = f"127.0.0.1:{port}"
            cmd = sys.executable if rt["command"] == "python" else rt["command"]
            if not os.path.isabs(cmd) and (REPO / cmd).exists():
                cmd = str(REPO / cmd)
            env = {k: v for k, v in os.environ.items() if not k.startswith(("UAR_", "ANTHROPIC_", "OPENAI_"))}
            env.update({"UAR_PLUGIN_ADDR": self.address, "PYTHONUNBUFFERED": "1",
                        "PYTHONPATH": os.pathsep.join([str(REPO / "plugin-sdks" / "python"), str(REPO / "runtime")])})
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            self.proc = subprocess.Popen([cmd, *rt.get("args", [])], cwd=str(REPO / rt.get("workdir", ".")), env=env,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         creationflags=flags, start_new_session=os.name != "nt")
        else:  # container: locked down; gRPC port published on loopback only
            port = _free_port()
            self.address = f"127.0.0.1:{port}"
            self.container = f"uar-plugin-{self.plugin_id.replace('.', '-')}-{self.version.replace('.', '-')}-{secrets.token_hex(3)}"
            args = ["docker", "run", "-d", "--rm", "--name", self.container, "--label", "uar.plugin=1",
                    "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64",
                    "--memory", limits.get("memory", "256m"), "--cpus", limits.get("cpus", "0.5"),
                    "--tmpfs", "/tmp:size=16m", "--user", "10001:10001",
                    "-p", f"127.0.0.1:{port}:50051", "-e", "UAR_PLUGIN_ADDR=0.0.0.0:50051", rt["image"], *rt.get("args", [])]
            r = await asyncio.to_thread(subprocess.run, args, capture_output=True, text=True)
            if r.returncode != 0:
                raise UARError("failed_precondition", f"container failed to start: {r.stderr.strip()[:300]}")
        self.channel = grpc.aio.insecure_channel(self.address)
        self.stub = ppbg.PluginStub(self.channel)
        deadline = time.monotonic() + 30
        while True:
            try:
                self.descriptor = await self.stub.Describe(ppb.DescribeRequest(), timeout=2)
                return
            except grpc.aio.AioRpcError:
                if self.proc and self.proc.poll() is not None:
                    raise UARError("failed_precondition", f"plugin process exited with code {self.proc.returncode}")
                if time.monotonic() > deadline:
                    raise UARError("failed_precondition", "plugin did not answer Describe within 30 s") from None
                await asyncio.sleep(0.2)

    async def stop(self) -> None:
        try:
            if self.stub:
                await self.stub.Shutdown(ppb.ShutdownRequest(grace_ms=500), timeout=1)
        except Exception:
            pass
        if self.channel:
            await self.channel.close()
        if self.proc and self.proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)
            else:
                self.proc.terminate()
        if self.container:
            await asyncio.to_thread(subprocess.run, ["docker", "rm", "-f", self.container], capture_output=True)

    async def call(self, method: str, request, stream: bool = False):
        if self.breaker_open():
            raise UARError("unavailable", f"plugin {self.plugin_id} is failing; circuit open")
        assert self.sem is not None and self.stub is not None
        async with self.sem:
            try:
                if stream:
                    return getattr(self.stub, method)(request, timeout=self.timeout_s)
                res = await getattr(self.stub, method)(request, timeout=self.timeout_s)
            except grpc.aio.AioRpcError as e:
                self.record(False)
                if e.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                    raise UARError("deadline_exceeded", f"plugin {self.plugin_id} timed out after {self.timeout_s}s") from None
                raise UARError("unavailable", f"plugin {self.plugin_id} failed: {e.code().name}") from None
        self.record(True)
        return res


# ------------------------------------------------------------------------------------ manager

class PluginManager:
    def __init__(self, store: Store, audit: Audit):
        self.store = store
        self.audit = audit
        self.instances: dict[tuple[str, str], Instance] = {}
        self.active: dict[str, str] = {}          # plugin_id -> version
        self.manifests: dict[tuple[str, str], dict] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.integrations: list = []              # callbacks(instance) run on activation

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> dict[str, str]:
        status: dict[str, str] = {}
        for row in await self.store.fetchall("SELECT a.plugin_id, a.version, v.manifest FROM plugin_active a "
                                             "JOIN plugin_versions v USING (plugin_id, version)"):
            self.manifests[(row["plugin_id"], row["version"])] = row["manifest"]
            try:
                inst = await self.instance(row["plugin_id"], row["version"])
                self.active[row["plugin_id"]] = row["version"]
                await self._integrate(inst)
                status[row["plugin_id"]] = f"active {row['version']}"
            except UARError as e:
                status[row["plugin_id"]] = f"failed: {e.message}"
                log.error("plugin %s@%s failed to start: %s", row["plugin_id"], row["version"], e.message)
        return status

    async def stop(self) -> None:
        await asyncio.gather(*(i.stop() for i in self.instances.values()), return_exceptions=True)
        self.instances.clear()

    # ------------------------------------------------------------ registry
    async def register(self, p: Principal, manifest: dict, request_id: str = "") -> dict:
        p.require("plugins:manage")
        validate_manifest(manifest)
        pid, ver = manifest["metadata"]["id"], manifest["metadata"]["version"]
        digest = manifest_digest(manifest)
        await self.audit.record(p, "plugins.register", f"{pid}@{ver}", "intent", request_id=request_id,
                                details={"digest": digest, "mode": manifest["spec"]["runtime"]["mode"]})
        row = await self.store.fetchone(
            "INSERT INTO plugin_versions (plugin_id, version, type, digest, manifest, created_by) VALUES "
            "(%s,%s,%s,%s,%s,%s) ON CONFLICT (plugin_id, version) DO NOTHING RETURNING *",
            pid, ver, manifest["spec"]["type"], digest, jsonb(manifest), p.subject)
        if row is None:
            row = await self.store.fetchone("SELECT * FROM plugin_versions WHERE plugin_id=%s AND version=%s", pid, ver)
            if row["digest"] != digest:
                raise UARError("conflict", f"{pid}@{ver} already exists with different content; versions are "
                               "immutable, bump the version")
        return await self._view(row)

    async def list(self, p: Principal) -> list[dict]:
        p.require("plugins:manage")
        rows = await self.store.fetchall("SELECT * FROM plugin_versions ORDER BY plugin_id, created_at")
        return [await self._view(r) for r in rows]

    async def _view(self, row: dict) -> dict:
        act = await self.store.fetchone("SELECT version, previous_version FROM plugin_active WHERE plugin_id=%s",
                                        row["plugin_id"])
        is_active = bool(act and act["version"] == row["version"])
        return {"plugin_id": row["plugin_id"], "version": row["version"], "type": row["type"],
                "status": row["status"], "digest": row["digest"], "active": is_active,
                "created_at": row["created_at"].isoformat().replace("+00:00", "Z"),
                "capabilities": sorted(declared_capabilities(row["manifest"])), "message": row["message"] or "",
                "previous_version": ((act or {}).get("previous_version") or "") if is_active else ""}

    async def activate(self, p: Principal, plugin_id: str, version: str, request_id: str = "",
                       action: str = "plugins.activate") -> dict:
        p.require("plugins:manage")
        row = await self.store.fetchone("SELECT * FROM plugin_versions WHERE plugin_id=%s AND version=%s",
                                        plugin_id, version)
        if row is None:
            raise not_found(f"plugin {plugin_id}@{version}")
        await self.audit.record(p, action, f"{plugin_id}@{version}", "intent", request_id=request_id)
        manifest = row["manifest"]
        self.manifests[(plugin_id, version)] = manifest
        try:
            await self._verify_integrity(manifest)
            inst = await self.instance(plugin_id, version)
            await self._integrate(inst)
        except UARError as e:
            await self.store.execute("UPDATE plugin_versions SET status='failed', message=%s WHERE plugin_id=%s "
                                     "AND version=%s", e.message[:500], plugin_id, version)
            await self._drop(plugin_id, version)
            await self.audit.record(p, action, f"{plugin_id}@{version}", "failed", request_id=request_id,
                                    details={"reason": e.message}, required=False)
            raise UARError("failed_precondition", f"activation failed: {e.message}") from None
        async with self.store.tx() as c:
            prev = await (await c.execute("SELECT version FROM plugin_active WHERE plugin_id=%s FOR UPDATE",
                                          (plugin_id,))).fetchone()
            prev_v = prev["version"] if prev else None
            await c.execute("INSERT INTO plugin_active (plugin_id, version, previous_version, activated_by) VALUES "
                            "(%s,%s,%s,%s) ON CONFLICT (plugin_id) DO UPDATE SET version=EXCLUDED.version, "
                            "previous_version=EXCLUDED.previous_version, activated_by=EXCLUDED.activated_by, "
                            "activated_at=now()", (plugin_id, version, prev_v if prev_v != version else None, p.subject))
            await c.execute("UPDATE plugin_versions SET status='inactive' WHERE plugin_id=%s AND version<>%s "
                            "AND status='active'", (plugin_id, version))
            await c.execute("UPDATE plugin_versions SET status='active', message=%s WHERE plugin_id=%s AND version=%s",
                            ("healthy", plugin_id, version))
        self.active[plugin_id] = version
        await self.audit.record(p, action, f"{plugin_id}@{version}", "succeeded", request_id=request_id,
                                required=False)
        row = await self.store.fetchone("SELECT * FROM plugin_versions WHERE plugin_id=%s AND version=%s",
                                        plugin_id, version)
        return await self._view(row)

    async def rollback(self, p: Principal, plugin_id: str, request_id: str = "") -> dict:
        p.require("plugins:manage")
        act = await self.store.fetchone("SELECT previous_version FROM plugin_active WHERE plugin_id=%s", plugin_id)
        if not act or not act["previous_version"]:
            raise UARError("failed_precondition", f"plugin {plugin_id} has no previous version to roll back to")
        return await self.activate(p, plugin_id, act["previous_version"], request_id, action="plugins.rollback")

    async def _verify_integrity(self, manifest: dict) -> None:
        rt = manifest["spec"]["runtime"]
        want = rt.get("digest")
        if rt["mode"] == "process" and rt.get("artifact"):
            if not want:
                raise UARError("failed_precondition", "runtime.digest is required when runtime.artifact is set")
            path = REPO / rt["artifact"]
            if not path.is_file():
                raise UARError("failed_precondition", f"artifact {rt['artifact']} not found")
            got = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            if got != want:
                raise UARError("failed_precondition", f"artifact digest mismatch (expected {want[:19]}…, got {got[:19]}…)")
        if rt["mode"] == "container":
            if not want:
                raise UARError("failed_precondition", "container plugins need runtime.digest (the image id)")
            r = await asyncio.to_thread(subprocess.run, ["docker", "image", "inspect", "--format", "{{.Id}}", rt["image"]],
                                        capture_output=True, text=True)
            if r.returncode != 0 or r.stdout.strip() != want:
                raise UARError("failed_precondition", "container image id does not match runtime.digest")

    # ------------------------------------------------------------ instances
    async def instance(self, plugin_id: str, version: str) -> Instance:
        key = (plugin_id, version)
        if key in self.instances and (not self.instances[key].proc or self.instances[key].proc.poll() is None):
            return self.instances[key]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self.instances and (not self.instances[key].proc or self.instances[key].proc.poll() is None):
                return self.instances[key]
            manifest = self.manifests.get(key)
            if manifest is None:
                row = await self.store.fetchone("SELECT manifest FROM plugin_versions WHERE plugin_id=%s AND version=%s",
                                                plugin_id, version)
                if row is None:
                    raise not_found(f"plugin {plugin_id}@{version}")
                manifest = self.manifests[key] = row["manifest"]
            inst = Instance(plugin_id, version, manifest)
            try:
                await inst.start()
                await self._handshake(inst)
            except BaseException:
                await inst.stop()
                raise
            self.instances[key] = inst
            return inst

    async def _handshake(self, inst: Instance) -> None:
        d, m = inst.descriptor, inst.manifest
        assert d is not None
        if d.id != m["metadata"]["id"] or d.version != m["metadata"]["version"] or d.kind != m["spec"]["type"]:
            raise UARError("failed_precondition", f"plugin describes itself as {d.kind} {d.id}@{d.version}, manifest "
                           f"says {m['spec']['type']} {m['metadata']['id']}@{m['metadata']['version']}")
        if m["spec"]["runtime"]["mode"] == "none":
            return
        extra = set(d.capabilities) - declared_capabilities(m)
        if extra:
            raise UARError("failed_precondition", f"plugin requests undeclared capabilities: {sorted(extra)}")
        spec = m["spec"]
        config = spec.get("config") or {}
        if spec.get("config_schema"):
            try:
                jsonschema.validate(config, spec["config_schema"])
            except jsonschema.ValidationError as e:
                raise UARError("failed_precondition", f"config invalid: {e.message}") from None
        secret_values = {n: os.environ[n] for n in (spec.get("capabilities") or {}).get("secrets", []) if n in os.environ}
        res = await inst.call("Init", ppb.PluginInitRequest(config=to_struct(config), secrets=secret_values))
        if not res.ok:
            raise UARError("failed_precondition", f"plugin Init refused: {res.message}")
        h = await inst.call("Health", ppb.HealthRequest())
        if not h.serving:
            raise UARError("failed_precondition", f"plugin not healthy: {h.message}")

    async def _drop(self, plugin_id: str, version: str) -> None:
        inst = self.instances.pop((plugin_id, version), None)
        if inst:
            await inst.stop()

    async def _integrate(self, inst: Instance) -> None:
        for fn in self.integrations:
            await fn(inst)

    def pins(self) -> dict[str, str]:
        return dict(self.active)

    def version_for(self, plugin_id: str) -> str:
        """The version this call should use: the run's pin if any, else the active version."""
        v = current_pins.get().get(plugin_id) or self.active.get(plugin_id)
        if not v:
            raise UARError("unavailable", f"plugin {plugin_id} is not active")
        return v

    def active_manifests(self, kind: str) -> list[dict]:
        return [self.manifests[(pid, v)] for pid, v in self.active.items()
                if (pid, v) in self.manifests and self.manifests[(pid, v)]["spec"]["type"] == kind]

    def execution_context(self, request_id: str = "", run_id: str = "", tenant: str = "") -> ppb.ExecutionContext:
        return ppb.ExecutionContext(tenant=tenant, run_id=run_id or "", request_id=request_id or "",
                                    deadline_unix_ms=int((time.time() + 600) * 1000))
