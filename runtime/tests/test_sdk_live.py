"""The SDK conformance suites (same fixtures as uar-mock) run against the live test runtime."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys

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
