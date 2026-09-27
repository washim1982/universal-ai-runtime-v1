"""UAR setup dashboard: a local web page with a Run button for every setup and test step, and a
live view of which services are up.

    Windows: double-click setup.cmd in the project folder
    Any OS:  python setup-dashboard/dashboard.py [--port 7070] [--no-browser]

Uses only the Python standard library, so it works on a fresh PC before anything is installed.

Safety
- Listens on 127.0.0.1 only and checks the Host header (no DNS-rebinding access).
- Every API call needs a random per-process session token sent in a custom header. The page gets
  it from /api/session, which other websites cannot read (no CORS headers, Host check, and
  requests the browser marks as cross-site are refused). Custom headers also force a CORS
  preflight that this server never approves, so other sites cannot send commands.
- Only the predefined steps below can run; the browser never sends a command line.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
WINDOWS = os.name == "nt"
VENV_PY = ROOT / ".venv" / ("Scripts/python.exe" if WINDOWS else "bin/python")
COMPOSE = ["docker", "compose", "-f", "deploy/docker/compose.yaml"]
MODEL = os.environ.get("UAR_SETUP_MODEL", "granite4:latest")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
TOKEN = secrets.token_urlsafe(24)


def which(name: str) -> str | None:
    return shutil.which(name)


def npm() -> str:
    return which("npm.cmd") or which("npm") or "npm"


# ----------------------------------------------------------------------------------------- steps

@dataclass
class Step:
    id: str
    group: str             # setup | run | test
    title: str
    summary: str
    commands: list          # list of (argv list, cwd relative to ROOT or None)
    optional: bool = False
    long: str = ""          # how long it usually takes
    needs: tuple = ()       # prerequisite ids that must be OK


def steps() -> list[Step]:
    py = str(VENV_PY)
    sys_py = sys.executable
    return [
        Step("venv", "setup", "Create the Python environment",
             "Makes a private Python environment in .venv so nothing is installed system-wide.",
             [([sys_py, "-m", "venv", ".venv"], None)], long="~20 s"),
        Step("install", "setup", "Install the runtime and the Python SDK",
             "Downloads and installs the runtime's libraries into .venv.",
             [([py, "-m", "pip", "install", "--upgrade", "pip"], None),
              ([py, "-m", "pip", "install", "-e", ".[dev]", "-e", "sdks/python"], None)], long="2–5 min"),
        Step("fixtures", "setup", "Create the demo data",
             "Writes the sample sales database and the product FAQ that the example agents use.",
             [([py, "scripts/make_fixtures.py"], None)], long="~2 s"),
        Step("config", "setup", "Create your configuration and API keys",
             "Writes .local/uar.yaml and your private keys in .local/credentials.env. Existing keys are kept.",
             [([py, "scripts/bootstrap_local.py"], None)], long="~2 s"),
        Step("model", "setup", f"Download the local AI model ({MODEL})",
             "Asks Ollama to download the small default model (about 2 GB).",
             [(["ollama", "pull", MODEL], None)], long="1–10 min", needs=("ollama_running",)),
        Step("postgres", "setup", "Start the database",
             "Starts PostgreSQL in Docker on 127.0.0.1:55440. Data is kept in a Docker volume.",
             [(COMPOSE + ["up", "-d", "postgres"], None)], long="~30 s first time", needs=("docker_running",)),
        Step("check", "setup", "Check the configuration",
             "Validates .local/uar.yaml.",
             [([py, "-m", "uar_runtime.main", "check-config", "--config", ".local/uar.yaml"], None)], long="~2 s"),
        Step("tssdk", "setup", "Build the TypeScript SDK",
             "Only needed for JavaScript/TypeScript apps and the TypeScript tests.",
             [([npm(), "install", "--no-audit", "--no-fund"], "sdks/typescript"),
              ([npm(), "run", "build"], "sdks/typescript")], optional=True, long="~30 s", needs=("node",)),
        Step("image", "setup", "Build the container image",
             "Only needed to run the runtime in Docker or Kubernetes.",
             [(["docker", "build", "-f", "deploy/docker/Dockerfile", "-t", "uar-runtime:0.6.0", "."], None)],
             optional=True, long="1–3 min", needs=("docker_running",)),
        # --- run
        Step("compose_up", "run", "Start the runtime in Docker (with tracing)",
             "Runs the container image with Postgres and Jaeger. Stop the local runtime first.",
             [(COMPOSE + ["--profile", "full", "up", "-d"], None)], optional=True, needs=("docker_running",)),
        Step("compose_down", "run", "Stop the Docker runtime",
             "Stops the runtime and Jaeger containers. The database keeps running.",
             [(COMPOSE + ["--profile", "full", "stop", "uar", "jaeger"], None)], optional=True),
        Step("postgres_stop", "run", "Stop the database",
             "Stops PostgreSQL. Your data stays in the Docker volume.",
             [(COMPOSE + ["stop", "postgres"], None)], optional=True),
        # --- test
        Step("smoke", "test", "End-to-end check (27 checks)",
             "Tests a running runtime over HTTP, streaming, gRPC and WebSocket, plus tools, agents and dry-run.",
             [([py, "scripts/smoke_test.py"], None)], long="1–2 min"),
        Step("pytest", "test", "Main automated test suite",
             "About 100 tests: security, recovery, routing, tools, agents, dry-run. Needs the database.",
             [([py, "-m", "pytest", "-q", "-p", "no:cacheprovider"], None)], long="~20 s"),
        Step("sdk_py", "test", "Python SDK tests", "Runs the Python SDK against the built-in mock server.",
             [([py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", "sdks/python/tests"], None)],
             long="~2 s"),
        Step("sdk_ts", "test", "TypeScript SDK tests", "Runs the TypeScript SDK against the mock server.",
             [([npm(), "test"], "sdks/typescript")], optional=True, long="~2 s", needs=("node",)),
        Step("docker_test", "test", "Container sandbox test",
             "Checks that tools run locked down inside a container (needs the container image).",
             [([py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "docker", "-o", "addopts=",
                "runtime/tests/test_mcp.py"], None)], optional=True, long="~5 s", needs=("docker_running",)),
        Step("live", "test", "Tests with real AI models",
             "Uses Ollama (and LM Studio / llama.cpp if running). LM Studio may load a large model.",
             [([py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "live", "-o", "addopts=", "-rs",
                "runtime/tests/test_live.py"], None)], optional=True, long="1–6 min", needs=("ollama_running",)),
    ]


STEP_MAP: dict[str, Step] = {}
SETUP_ORDER = ["venv", "install", "fixtures", "config", "model", "postgres", "check"]


# ----------------------------------------------------------------------------------------- jobs

@dataclass
class Job:
    id: str
    title: str
    lines: list = field(default_factory=list)
    done: bool = False
    rc: int | None = None
    started: float = field(default_factory=time.time)
    ended: float | None = None
    step_ids: list = field(default_factory=list)
    proc: subprocess.Popen | None = None


JOBS: dict[str, Job] = {}
LAST_RESULT: dict[str, dict] = {}    # step id -> {"ok": bool, "at": ts}
RUNNING_STEPS: set[str] = set()
LOCK = threading.Lock()


def child_env() -> dict:
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PIP_PROGRESS_BAR": "off",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1", "NO_COLOR": "1", "FORCE_COLOR": "0"})
    return env


def _stream(job: Job, argv: list, cwd: str | None) -> int:
    job.lines.append(f"$ {' '.join(str(a) for a in argv)}")
    try:
        proc = subprocess.Popen(argv, cwd=str(ROOT / cwd) if cwd else str(ROOT), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=child_env(), stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        job.lines.append(f"!! '{argv[0]}' was not found. Install it first (see Prerequisites).")
        return 127
    job.proc = proc
    assert proc.stdout is not None
    for raw in iter(proc.stdout.readline, b""):
        line = ANSI.sub("", raw.decode("utf-8", errors="replace")).rstrip("\r\n")
        for part in line.split("\r"):
            if part.strip():
                job.lines.append(part)
        if len(job.lines) > 5000:
            del job.lines[:1000]
    return proc.wait()


def start_job(title: str, step_ids: list[str]) -> Job:
    job = Job(secrets.token_hex(6), title, step_ids=step_ids)
    with LOCK:
        busy = [s for s in step_ids if s in RUNNING_STEPS]
        if busy:
            raise ValueError(f"already running: {', '.join(busy)}")
        RUNNING_STEPS.update(step_ids)
        JOBS[job.id] = job

    def work() -> None:
        rc = 0
        try:
            for sid in step_ids:
                st = STEP_MAP[sid]
                job.lines.append(f"== {st.title}")
                missing = [n for n in st.needs if not status_cache.get().get("prereq", {}).get(n, {}).get("ok")]
                if missing:
                    job.lines.append(f"!! Not ready: {', '.join(PREREQ_LABEL[m] for m in missing)}. "
                                     "Fix it in Prerequisites / Services, then run again.")
                    rc = 2
                for argv, cwd in (st.commands if rc == 0 else []):
                    rc = _stream(job, argv, cwd)
                    if rc != 0:
                        break
                LAST_RESULT[sid] = {"ok": rc == 0, "at": time.time()}
                job.lines.append(f"== {'OK' if rc == 0 else f'FAILED (exit code {rc})'}: {st.title}")
                if rc != 0:
                    break
        except Exception as e:  # pragma: no cover - defensive
            job.lines.append(f"!! {type(e).__name__}: {e}")
            rc = 1
        finally:
            job.rc, job.done, job.ended = rc, True, time.time()
            with LOCK:
                RUNNING_STEPS.difference_update(step_ids)
            status_cache.invalidate()

    threading.Thread(target=work, daemon=True).start()
    return job


# ----------------------------------------------------------------------------------------- managed runtime

class ManagedRuntime:
    """`uar serve` started by the dashboard; stopped with the dashboard or the Stop button."""

    def __init__(self) -> None:
        self.job: Job | None = None

    def running(self) -> bool:
        return bool(self.job and self.job.proc and self.job.proc.poll() is None)

    def start(self) -> Job:
        if self.running():
            raise ValueError("the local runtime is already running")
        if port_open(9000):
            raise ValueError("port 9000 is in use (is the Docker runtime running? stop it first)")
        job = Job(secrets.token_hex(6), "Local runtime (uar serve)")
        JOBS[job.id] = job
        argv = [str(VENV_PY), "-m", "uar_runtime.main", "serve", "--config", ".local/uar.yaml"]
        threading.Thread(target=lambda: self._run(job, argv), daemon=True).start()
        self.job = job
        return job

    def _run(self, job: Job, argv: list) -> None:
        job.rc = _stream(job, argv, None)
        job.done, job.ended = True, time.time()
        job.lines.append(f"== runtime exited (code {job.rc})")
        status_cache.invalidate()

    def stop(self) -> None:
        if not self.running():
            return
        pid = self.job.proc.pid
        if WINDOWS:  # kill the whole tree (the runtime starts MCP server processes)
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            self.job.proc.terminate()
        status_cache.invalidate()


RUNTIME = ManagedRuntime()


# ----------------------------------------------------------------------------------------- status

PREREQ_LABEL = {"python": "Python 3.12+", "docker": "Docker installed", "docker_running": "Docker running",
                "node": "Node.js", "ollama": "Ollama installed", "ollama_running": "Ollama running",
                "model": f"Model {MODEL}", "winget": "winget"}


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def http_json(url: str, timeout: float = 1.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except Exception:
            return e.code, None
    except Exception:
        return None, None


def run_quiet(argv: list, timeout: float = 8) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, encoding="utf-8",
                           errors="replace", cwd=str(ROOT))
        return p.returncode, (p.stdout + p.stderr).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return 127, type(e).__name__


def compute_status() -> dict:
    pyv = sys.version_info
    prereq: dict[str, dict] = {}
    prereq["python"] = {"ok": pyv >= (3, 12), "detail": platform.python_version(),
                        "fix": "Install Python 3.12 or newer, then start the dashboard with it."}
    docker = which("docker")
    prereq["docker"] = {"ok": bool(docker), "detail": "installed" if docker else "not found",
                        "install": "Docker.DockerDesktop"}
    rc, out = run_quiet(["docker", "info", "--format", "{{.ServerVersion}}"]) if docker else (1, "")
    prereq["docker_running"] = {"ok": rc == 0, "detail": f"engine {out}" if rc == 0 else "not running",
                                "action": "start_docker" if docker else None}
    node = which("node")
    nrc, nout = run_quiet(["node", "--version"]) if node else (1, "")
    prereq["node"] = {"ok": nrc == 0, "detail": nout if nrc == 0 else "not found (optional)",
                      "install": "OpenJS.NodeJS.LTS", "optional": True}
    ollama = which("ollama")
    prereq["ollama"] = {"ok": bool(ollama), "detail": "installed" if ollama else "not found",
                        "install": "Ollama.Ollama"}
    code, tags = http_json("http://127.0.0.1:11434/api/tags")
    models = [m.get("name", "") for m in (tags or {}).get("models", [])] if code == 200 else []
    prereq["ollama_running"] = {"ok": code == 200, "detail": f"{len(models)} models" if code == 200 else "not running",
                                "action": "start_ollama" if ollama else None}
    has_model = any(m == MODEL or m.split(":")[0] == MODEL.split(":")[0] for m in models)
    prereq["model"] = {"ok": has_model, "detail": "downloaded" if has_model else "not downloaded",
                       "step": "model"}
    prereq["winget"] = {"ok": bool(which("winget")), "detail": "available" if which("winget") else "not available",
                        "hidden": True}

    # services
    containers = []
    if prereq["docker_running"]["ok"]:
        rc, out = run_quiet(["docker", "ps", "--format", "{{.Names}}"])
        containers = out.split() if rc == 0 else []
    rcode, ready = http_json("http://127.0.0.1:9000/readyz")
    runtime_mode = ("dashboard" if RUNTIME.running() else "docker" if "uar-uar-1" in containers
                    else "external" if rcode else None)
    mcp = (ready or {}).get("mcp", {}) if isinstance(ready, dict) else {}
    services = [
        {"id": "postgres", "name": "Database (PostgreSQL)", "where": "127.0.0.1:55440", "up": port_open(55440),
         "required": True, "start": "postgres", "stop": "postgres_stop"},
        {"id": "ollama", "name": "Ollama (local models)", "where": "127.0.0.1:11434", "up": prereq["ollama_running"]["ok"],
         "required": True, "action": "start_ollama" if ollama else None},
        {"id": "runtime", "name": "UAR runtime (HTTP, SSE, WebSocket)", "where": "http://127.0.0.1:9000",
         "up": rcode == 200, "required": True, "mode": runtime_mode,
         "detail": "ready" if rcode == 200 else ("starting / not ready" if rcode else "stopped")},
        {"id": "grpc", "name": "UAR runtime (gRPC)", "where": "127.0.0.1:50051", "up": port_open(50051), "required": True},
        {"id": "mcp_fs", "name": "Tool server: files", "where": "managed by the runtime", "up": bool(mcp.get("fs")),
         "required": True},
        {"id": "mcp_db", "name": "Tool server: sales database", "where": "managed by the runtime",
         "up": bool(mcp.get("db")), "required": True},
        {"id": "jaeger", "name": "Jaeger (traces)", "where": "http://127.0.0.1:16686", "up": port_open(16686),
         "required": False, "link": "http://127.0.0.1:16686"},
        {"id": "lmstudio", "name": "LM Studio", "where": "127.0.0.1:1234", "up": port_open(1234), "required": False},
        {"id": "llamacpp", "name": "llama.cpp server", "where": "127.0.0.1:8080", "up": port_open(8080),
         "required": False},
    ]

    done = {
        "venv": VENV_PY.exists(),
        "install": VENV_PY.exists() and (LAST_RESULT.get("install", {}).get("ok") or _installed()),
        "fixtures": (ROOT / "examples/fixtures/sales.db").exists() and (ROOT / "examples/workspace/docs/product-faq.md").exists(),
        "config": (ROOT / ".local/uar.yaml").exists() and (ROOT / ".local/credentials.env").exists(),
        "model": has_model,
        "postgres": services[0]["up"],
        "check": _config_valid(),
        "tssdk": (ROOT / "sdks/typescript/dist/index.js").exists(),
        "image": _image_exists() if prereq["docker_running"]["ok"] else False,
    }
    return {"prereq": prereq, "services": services, "done": done, "last": LAST_RESULT,
            "running": sorted(RUNNING_STEPS), "runtime_job": RUNTIME.job.id if RUNTIME.running() else None,
            "platform": platform.platform(), "root": str(ROOT)}


_INSTALLED = {"ok": False, "at": 0.0}


def _installed() -> bool:
    if _INSTALLED["ok"] or time.time() - _INSTALLED["at"] < 20:
        return _INSTALLED["ok"]
    rc, _ = run_quiet([str(VENV_PY), "-c", "import uar_runtime, uar, grpc, psycopg"], timeout=20)
    _INSTALLED.update(ok=rc == 0, at=time.time())
    return rc == 0


_CHECK = {"ok": False, "at": 0.0}


def _config_valid() -> bool:
    if not (VENV_PY.exists() and (ROOT / ".local/uar.yaml").exists()):
        return False
    if time.time() - _CHECK["at"] < 20:
        return _CHECK["ok"]
    rc, _ = run_quiet([str(VENV_PY), "-m", "uar_runtime.main", "check-config", "--config", ".local/uar.yaml"],
                      timeout=30)
    _CHECK.update(ok=rc == 0, at=time.time())
    return _CHECK["ok"]


_IMAGE = {"ok": False, "at": 0.0}


def _image_exists() -> bool:
    if time.time() - _IMAGE["at"] < 15:
        return _IMAGE["ok"]
    rc, _ = run_quiet(["docker", "image", "inspect", "uar-runtime:0.6.0"])
    _IMAGE.update(ok=rc == 0, at=time.time())
    return rc == 0


class StatusCache:
    def __init__(self) -> None:
        self.value: dict = {}
        self.at = 0.0
        self.lock = threading.Lock()

    def get(self) -> dict:
        with self.lock:
            if time.time() - self.at > 2.5 or not self.value:
                self.value = compute_status()
                self.at = time.time()
            return self.value

    def invalidate(self) -> None:
        self.at = 0.0
        _INSTALLED["at"] = 0.0
        _IMAGE["at"] = 0.0
        _CHECK["at"] = 0.0


status_cache = StatusCache()


# ----------------------------------------------------------------------------------------- actions

def start_docker() -> str:
    candidates = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Docker/Docker/Docker Desktop.exe"]
    for c in candidates:
        if c.exists():
            subprocess.Popen([str(c)], close_fds=True)
            return "Starting Docker Desktop… this can take a minute."
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-a", "Docker"])
        return "Starting Docker Desktop…"
    raise ValueError("Docker Desktop was not found. Install it first.")


def start_ollama() -> str:
    app = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama app.exe"
    if WINDOWS and app.exists():
        subprocess.Popen([str(app)], close_fds=True)
        return "Starting Ollama…"
    if which("ollama"):
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, start_new_session=not WINDOWS)
        return "Starting Ollama…"
    raise ValueError("Ollama was not found. Install it first.")


WINGET_IDS = {"Docker.DockerDesktop", "OpenJS.NodeJS.LTS", "Ollama.Ollama"}


def install_with_winget(pkg: str) -> Job:
    if pkg not in WINGET_IDS or not which("winget"):
        raise ValueError("winget install is not available for this item")
    job = Job(secrets.token_hex(6), f"Install {pkg}")
    JOBS[job.id] = job

    def work() -> None:
        job.lines.append("Windows may ask for permission (UAC). Accept it in the prompt that appears.")
        job.rc = _stream(job, ["winget", "install", "-e", "--id", pkg, "--accept-package-agreements",
                               "--accept-source-agreements"], None)
        job.lines.append("== finished. If a restart or sign-out is requested, do that, then reopen the dashboard.")
        job.done, job.ended = True, time.time()
        status_cache.invalidate()

    threading.Thread(target=work, daemon=True).start()
    return job


def dev_keys() -> dict:
    p = ROOT / ".local/credentials.env"
    if not p.exists():
        return {}
    kv = dict(line.split("=", 1) for line in p.read_text(encoding="utf-8").splitlines() if "=" in line)
    return {k: v for k, v in kv.items() if k.endswith("_KEY")}


# ----------------------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "uar-setup/1.0"

    def log_message(self, fmt, *args):  # quiet console
        pass

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def _auth(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Setup-Token", ""), TOKEN)

    def _same_origin(self) -> bool:
        # Browsers label requests from other websites "cross-site"; refuse those outright.
        return self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")

    def _send(self, code: int, body, ctype: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if u.path == "/guide":
            return self._send(200, (ROOT / "docs/user-guide.html").read_bytes(), "text/html; charset=utf-8")
        m = re.fullmatch(r"/(?:guide/)?img/([a-z0-9-]+\.png)", u.path)
        if m and (ROOT / "docs/img" / m.group(1)).is_file():  # the guide's screenshots only
            return self._send(200, (ROOT / "docs/img" / m.group(1)).read_bytes(), "image/png")
        if u.path == "/api/session":
            if not self._same_origin():
                return self._send(403, {"error": "cross-site request refused"})
            return self._send(200, {"token": TOKEN})
        if not self._auth():
            return self._send(401, {"error": "missing or wrong session token"})
        if u.path == "/api/status":
            return self._send(200, status_cache.get())
        if u.path == "/api/steps":
            return self._send(200, [{"id": s.id, "group": s.group, "title": s.title, "summary": s.summary,
                                     "optional": s.optional, "long": s.long,
                                     "commands": [" ".join(str(a) for a in argv) for argv, _ in s.commands]}
                                    for s in STEP_MAP.values()])
        if u.path.startswith("/api/job/"):
            job = JOBS.get(u.path.rsplit("/", 1)[-1])
            if not job:
                return self._send(404, {"error": "no such job"})
            since = int(parse_qs(u.query).get("since", ["0"])[0])
            return self._send(200, {"id": job.id, "title": job.title, "lines": job.lines[since:],
                                    "next": len(job.lines), "done": job.done, "rc": job.rc})
        if u.path == "/api/keys":
            return self._send(200, dev_keys())
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._host_ok() or not self._same_origin():
            return self._send(403, {"error": "request refused"})
        if not self._auth():
            return self._send(401, {"error": "missing or wrong session token"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        path = urlparse(self.path).path
        try:
            if path == "/api/run":
                sid = body.get("step")
                if sid not in STEP_MAP:
                    return self._send(400, {"error": "unknown step"})
                job = start_job(STEP_MAP[sid].title, [sid])
                return self._send(200, {"job": job.id})
            if path == "/api/run-setup":
                status = status_cache.get()
                todo = [s for s in SETUP_ORDER if not status["done"].get(s)] or ["check"]
                job = start_job("Complete setup", todo)
                return self._send(200, {"job": job.id, "steps": todo})
            if path == "/api/runtime/start":
                return self._send(200, {"job": RUNTIME.start().id})
            if path == "/api/runtime/stop":
                RUNTIME.stop()
                return self._send(200, {"ok": True})
            if path == "/api/action":
                name = body.get("name")
                if name == "start_docker":
                    msg = start_docker()
                elif name == "start_ollama":
                    msg = start_ollama()
                elif name == "install":
                    return self._send(200, {"job": install_with_winget(body.get("package", "")).id})
                else:
                    return self._send(400, {"error": "unknown action"})
                status_cache.invalidate()
                return self._send(200, {"message": msg})
            if path == "/api/job/stop":
                job = JOBS.get(body.get("job", ""))
                if job and job.proc and job.proc.poll() is None:
                    if WINDOWS:
                        subprocess.run(["taskkill", "/PID", str(job.proc.pid), "/T", "/F"], capture_output=True)
                    else:
                        job.proc.terminate()
                return self._send(200, {"ok": True})
        except ValueError as e:
            return self._send(409, {"error": str(e)})
        self._send(404, {"error": "not found"})


class ExclusiveServer(ThreadingHTTPServer):
    """Refuse to share the port. (On Windows, SO_REUSEADDR would let two dashboards bind it.)"""
    allow_reuse_address = not WINDOWS

    def server_bind(self) -> None:
        if WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7070)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--token", help=argparse.SUPPRESS)
    a = ap.parse_args()
    global TOKEN
    if a.token:
        TOKEN = a.token
    STEP_MAP.update({s.id: s for s in steps()})
    url = f"http://127.0.0.1:{a.port}/"
    try:
        server = ExclusiveServer(("127.0.0.1", a.port), Handler)
    except OSError:
        print(f"\n  Port {a.port} is already in use - the setup dashboard is probably already running.", flush=True)
        print(f"  Opening {url}  (to use another port: setup.cmd --port 7080)\n", flush=True)
        if not a.no_browser:
            webbrowser.open(url)
        sys.exit(1)
    print("\n  UAR setup dashboard is running.")
    print(f"  Open: {url}", flush=True)
    print("  Keep this window open. Press Ctrl+C to stop (a runtime started from the dashboard stops too).\n")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        RUNTIME.stop()
        server.server_close()


if __name__ == "__main__":
    main()
