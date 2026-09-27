"""The SDK conformance suites (same fixtures as uar-mock) run against the live test runtime."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT


def _run(cmd: list[str], env_extra: dict, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env={**os.environ, **env_extra}, capture_output=True, text=True, timeout=300)


async def test_python_sdk_against_live_runtime(env):
    r = await asyncio.to_thread(_run, [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=",
                                       "sdks/python/tests"],
                                {"UAR_LIVE_URL": env.http, "UAR_LIVE_KEY": env.keys.dev}, ROOT)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]


async def test_typescript_sdk_against_live_runtime(env):
    ts = ROOT / "sdks" / "typescript"
    if not (ts / "dist" / "index.js").exists() or not shutil.which("node"):
        pytest.skip("TypeScript SDK not built (npm install && npm run build in sdks/typescript)")
    r = await asyncio.to_thread(_run, ["node", "--test", "test/conformance.test.mjs"],
                                {"UAR_LIVE_URL": env.http, "UAR_LIVE_KEY": env.keys.dev}, ts)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]


def _tool(name: str, *extra: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for cand in extra:
        if cand and Path(cand).exists():
            return cand
    return None


GO = _tool("go", r"C:\Program Files\Go\bin\go.exe")
CARGO = _tool("cargo")
DOTNET = _tool("dotnet")
MVN = _tool("mvn", os.environ.get("UAR_MVN", ""), str(Path.home() / "tools/apache-maven-3.9.16/bin/mvn.cmd"))


def _live(env) -> dict:
    return {"UAR_LIVE_URL": env.http, "UAR_LIVE_KEY": env.keys.dev}


@pytest.mark.skipif(not GO, reason="Go not installed")
async def test_go_sdk_against_live_runtime(env):
    r = await asyncio.to_thread(_run, [GO, "test", "-count=1", "./..."], _live(env), ROOT / "sdks/go")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]


@pytest.mark.skipif(not CARGO, reason="Rust not installed")
async def test_rust_sdk_against_live_runtime(env):
    r = await asyncio.to_thread(_run, [CARGO, "test", "--quiet"], _live(env), ROOT / "sdks/rust")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]


@pytest.mark.skipif(not DOTNET, reason=".NET SDK not installed")
async def test_dotnet_sdk_against_live_runtime(env):
    r = await asyncio.to_thread(_run, [DOTNET, "run", "--project", "Uar.Client.Conformance"], _live(env),
                                ROOT / "sdks/dotnet")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]


@pytest.mark.skipif(not MVN, reason="Maven not installed")
async def test_java_sdk_against_live_runtime(env):
    extra = _live(env)
    if not os.environ.get("JAVA_HOME"):
        jdks = sorted(Path(r"C:\Program Files\Microsoft").glob("jdk-21*")) if os.name == "nt" else []
        if jdks:
            extra["JAVA_HOME"] = str(jdks[-1])
    if os.name == "nt":  # trust what Windows trusts (e.g. a TLS-inspecting proxy's root CA)
        extra["MAVEN_OPTS"] = "-Djavax.net.ssl.trustStoreType=Windows-ROOT"
    r = await asyncio.to_thread(_run, [MVN, "-q", "-B", "test"], extra, ROOT / "sdks/java")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
