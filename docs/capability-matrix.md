# Capability matrix

Status of every UAR capability from `Mission_Comparison.md`, as of 2026-09-27 (M9 + admin app).
Only **live-tested** and **fixture-tested** entries may be claimed as supported.

| Legend | Meaning |
|---|---|
| live-tested | exercised against the real dependency on the reference machine |
| fixture-tested | verified with deterministic fakes, recorded fixtures or real local processes |
| implemented | code exists; its gate test is not yet in place |
| planned | milestone M7–M10 |

## Providers

| Provider | Class | Sync | Stream | Tools | JSON output | Status |
|---|---|---|---|---|---|---|
| Ollama | local | ✔ | ✔ | ✔ | ✔ (`format`) | **live-tested** (granite4) |
| LM Studio (OpenAI-compatible) | local | ✔ | ✔ | ✔ | ✔ | **live-tested** (qwen3.8-27b) sync + stream |
| llama.cpp `llama-server` (OpenAI-compatible) | local | ✔ | ✔ | ✔ | ✔ | implemented; catalog discovery live; chat gate pending API key |
| Anthropic Messages (official SDK) | cloud | ✔ | ✔ | ✔ | ✔ | fixture-tested (documented wire format); not live-tested |
| OpenAI Responses | cloud | ✔ | ✔ | ✔ | ✔ | fixture-tested (documented wire format); not live-tested |
| Groq (OpenAI-compatible) | cloud | ✔ | ✔ | ✔ | ✔ | fixture-tested (shared adapter, tool-call deltas); not live-tested |
| vLLM / enterprise gateways (OpenAI-compatible) | enterprise | ✔ | ✔ | ✔ | ✔ | implemented |
| Azure OpenAI (v1 and classic deployments API) | cloud | ✔ | ✔ | ✔ | ✔ | fixture-tested (documented wire format); not live-tested |
| Google Vertex AI (Gemini generateContent) | cloud | ✔ | ✔ | ✔ | ✔ | fixture-tested (documented wire format); not live-tested |
| Model plugins (gRPC, any language) | any | ✔ | ✔ | | | fixture-tested (Go reference plugin, Python test plugin) |

## Features

| Feature (comparison row) | Status |
|---|---|
| Multi-model routing, aliases, `local:`/`cloud:`/`enterprise:` classes | fixture-tested + live-tested (local) |
| Router rules (tenant / role / data class), capability matching, opt-in fallback, circuit breaker | fixture-tested |
| Router rules v2: data residency, provider regions, price caps, prefer cost / latency | fixture-tested (`test_router_rules.py`) |
| MCP gateway, orchestration, per-call authorization, audit, intents | fixture-tested (real MCP servers) |
| MCP server hosting: supervised stdio / container; Streamable HTTP | fixture-tested; Kubernetes Deployments written, not deployed |
| Tool sandboxing: roots, links, ADS, exclusive writes, read-only SQL, container isolation | fixture-tested |
| Native tool plugins | fixture-tested (TypeScript reference plugin, Python test plugin) |
| Agent gateway (start / get / watch / cancel / resolve on HTTP, gRPC, WebSocket) | fixture-tested |
| Durable engine: checkpoints, fencing, recovery, ambiguous writes, loops, parallel, sub-agents | fixture-tested + live-tested (report agent on granite4) |
| Approvals: approval nodes and inline tool approvals (bound to action + args hash, decided once, used once, expiry fails closed, sub-agent approvals park the parent) | fixture-tested (`test_governance.py`) |
| Dry-run: static plan + fixture simulation, non-executing | fixture-tested |
| Agent plugins (executable and declarative) | fixture-tested (Python reference plugin, declarative package) |
| SDKs: Python, TypeScript | fixture-tested (mock + live runtime) |
| SDKs: Go, Rust, .NET, Java | fixture-tested (mock + live runtime) |
| Plugin SDK helpers: Python, Go, TypeScript, .NET, Java | fixture-tested (a reference plugin per language) |
| Kubernetes-native / Helm / Docker Desktop Kubernetes | implemented, **not verified** (no cluster) |
| Local-only mode (`profile: local-only` blocks cloud routes) | fixture-tested via egress policy; container stack ran with cloud credentials absent |
| Plugin runtime (register, integrity, activate, pin, rollback, breaker) | fixture-tested |
| RBAC, tenant isolation, audit (fail-closed, payload-free, per-tenant SHA-256 hash chain, append-only trigger, verify + export API) | fixture-tested (tampering, deletion and truncation detected) |
| Model and tool usage policies, token budgets | fixture-tested |
| Agent sandboxing (declared permissions, narrowed sub-agent tools) | fixture-tested |
| Observability: OTel traces, Prometheus metrics, usage/cost ledger | fixture-tested + verified in Jaeger |
| SSO/OIDC (JWKS, key rotation, group and service-account role mapping), custom roles | fixture-tested against a local JWKS endpoint; not tested with a commercial IdP |
| Content redaction (audit, event log, approval summaries, prompts to chosen model classes) | fixture-tested (pattern-based detectors) |
| Retention and deletion jobs (runs, usage, idempotency, audit with chain anchor) | fixture-tested |
| Administration API: runtime info, inference history, API keys (create/revoke, hot reload), access policy, service logs | fixture-tested (`test_admin_api.py`) |
| Built-in token service (STS): application registration, OAuth 2.0 client credentials, ES256 access tokens on every endpoint, secret rotation, instant disable, signing-key rotation, encrypted keys, API-keys-off mode; SDK support (Python, TypeScript) | fixture-tested (`test_sts.py`, 14 tests) + live-tested on the Docker runtime (register, token, inference) |
| Windows admin app (UAR Admin, WPF) | built and exercised against a live 0.9 runtime: page snapshots and a harness driving start / stop / restart, approvals, keys, audit and logs |
