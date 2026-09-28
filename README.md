# Universal AI Runtime (UAR)

One protocol for model inference, governed MCP tools and durable agent graphs, with SDKs for
application developers. This repository implements milestones **M0–M8** of
[docs/UAR-MASTER-IMPLEMENTATION-PLAN.md](docs/UAR-MASTER-IMPLEMENTATION-PLAN.md).

**New PC? Double-click `setup.cmd`** for a setup dashboard with Run buttons and live service status; the friendly walkthrough is [docs/user-guide.html](docs/user-guide.html). Feature-by-feature testing: [docs/user-guide.md](docs/user-guide.md).

What is verified and what is not is recorded in [docs/gates/mvp-gates.md](docs/gates/mvp-gates.md)
and [docs/capability-matrix.md](docs/capability-matrix.md).

```python
from uar import Client

client = Client("http://localhost:9000")
resp = client.inference(model="local:default", prompt="Explain quantum computing", agent="in_app_assistant")
print(resp.text)
```

## What is in the box

| Area | Where |
|---|---|
| Canonical contract (protobuf) → OpenAPI, JSON Schema, TS types | `proto/`, `contracts/`, `scripts/gen_*.py` |
| Runtime: gRPC + HTTP/SSE + WebSocket on one service layer | `runtime/uar_runtime/` |
| Model router: `local:` / `cloud:` / `enterprise:` classes, aliases, policy rules, fallback, circuit breakers, price catalog, budgets | `runtime/uar_runtime/router/` |
| Adapters: Ollama, OpenAI-compatible (LM Studio, llama.cpp, Groq, vLLM), OpenAI Responses, Anthropic (official SDK), deterministic fake | `router/adapters/` |
| MCP orchestrator: admin-registered servers, stdio / container / Streamable HTTP, per-call authorization, audit, intents | `runtime/uar_runtime/mcp/` |
| First-party MCP servers: confined filesystem, read-only SQL | `mcp-servers/` |
| Durable agent engine: YAML/JSON graphs, CEL expressions, checkpoints, fencing, recovery, sub-agents, loops, parallel | `runtime/uar_runtime/engine/` |
| Dry-run: static plan + fixture simulation, provably non-executing | `runtime/uar_runtime/simulation/` |
| Observability: OpenTelemetry traces, Prometheus metrics, JSON logs, usage ledger | `observability.py` |
| SDKs: Python, TypeScript, Go, Rust, .NET, Java — one set of conformance fixtures, `uar-mock` | `sdks/`, `contracts/fixtures/`, `mock/` |
| Inference samples: one small project per language (Python, TypeScript, Go, Rust, C#, Java), API key or registered-application sign-in | `samples/` |
| Governance: approvals, hash-chained audit, OIDC SSO, redaction, retention, router rules v2 | `runtime/uar_runtime/` |
| Token service (STS): application registration, OAuth 2.0 client credentials, access tokens | `runtime/uar_runtime/sts.py`, [docs/sts.md](docs/sts.md) |
| Windows admin app (UAR Admin): runtime control, keys, applications, API reference, inference history, approvals, audit, logs | `admin-app/`, `admin.cmd` |
| Plugins: gRPC plugin runtime; plugin SDKs for Python, Go, TypeScript, .NET, Java; reference plugins | `runtime/uar_runtime/plugins/`, `plugin-sdks/`, `plugins/` |
| Packaging: Docker image, Compose, Helm chart (local profile), CI | `deploy/`, `.github/` |

## Quick start (Windows, PowerShell or Git Bash)

Prerequisites: Python 3.12+, Docker Desktop, Node 18+ (for the TypeScript SDK), and Ollama with
`granite4:latest` (or change `local:default` in the config).

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]" -e sdks/python
.venv/Scripts/python scripts/make_fixtures.py
.venv/Scripts/python scripts/bootstrap_local.py
docker compose -f deploy/docker/compose.yaml up -d postgres
.venv/Scripts/uar serve --config .local/uar.yaml
```

The developer API key is in `.local/credentials.env` (`UAR_DEV_KEY`). Then:

```bash
curl -s localhost:9000/api/v1/inference -H "X-API-Key: <UAR_DEV_KEY>" -H "Content-Type: application/json" -d "{\"model\":\"local:default\",\"input\":\"hello\"}"
```

Containerised instead: `docker build -f deploy/docker/Dockerfile -t uar-runtime:0.9.0 .` then
`docker compose -f deploy/docker/compose.yaml --profile full up -d` (runtime + Postgres + Jaeger at
http://localhost:16686). Kubernetes: [docs/runbooks/docker-desktop.md](docs/runbooks/docker-desktop.md).

## Tests

```bash
.venv/Scripts/python -m pytest                       # runtime suite (needs the compose postgres)
.venv/Scripts/python -m pytest -m live -o addopts=   # real Ollama / LM Studio / llama.cpp
.venv/Scripts/python -m pytest -m docker -o addopts= runtime/tests/test_mcp.py   # container sandbox
.venv/Scripts/python -m pytest -o addopts= sdks/python/tests                     # Python SDK vs uar-mock
cd sdks/typescript && npm install && npm run build && npm test                 # TS SDK vs uar-mock
```

## Trust boundaries (MVP)

Run on a trusted machine, API on loopback. Identity is tenant-bound API keys or dev HS256 JWTs
(OIDC is M9). Tools run as separate processes (or locked-down containers); hostile third-party
code is outside the supported trust model. Quotas are per process. See
[docs/threat-model.md](docs/threat-model.md).
