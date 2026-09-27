# Capability matrix

Status of every UAR capability from `Mission_Comparison.md`, as of 2026-09-26 (M6).
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
| Azure OpenAI, Google Vertex AI | cloud | | | | | planned (M7) |
| Model plugins (gRPC) | any | | | | | planned (M7) |

## Features

| Feature (comparison row) | Status |
|---|---|
| Multi-model routing, aliases, `local:`/`cloud:`/`enterprise:` classes | fixture-tested + live-tested (local) |
| Router rules (tenant / role / data class), capability matching, opt-in fallback, circuit breaker | fixture-tested |
| Cost/latency-aware selection, region rules | planned (M9) |
| MCP gateway, orchestration, per-call authorization, audit, intents | fixture-tested (real MCP servers) |
| MCP server hosting: supervised stdio / container; Streamable HTTP | fixture-tested; Kubernetes Deployments written, not deployed |
| Tool sandboxing: roots, links, ADS, exclusive writes, read-only SQL, container isolation | fixture-tested |
| Native tool plugins | planned (M7) |
| Agent gateway (start / get / watch / cancel / resolve on HTTP, gRPC, WebSocket) | fixture-tested |
| Durable engine: checkpoints, fencing, recovery, ambiguous writes, loops, parallel, sub-agents | fixture-tested + live-tested (report agent on granite4) |
| Approval nodes | planned (M9); contract present, returns 501 |
| Dry-run: static plan + fixture simulation, non-executing | fixture-tested |
| Agent plugins | planned (M7) |
| SDKs: Python, TypeScript | fixture-tested (mock + live runtime) |
| SDKs: Java, .NET, Go, Rust | planned (M8) |
| Kubernetes-native / Helm / Docker Desktop Kubernetes | implemented, **not verified** (no cluster) |
| Local-only mode (`profile: local-only` blocks cloud routes) | fixture-tested via egress policy; container stack ran with cloud credentials absent |
| Plugin runtime | planned (M7) |
| RBAC, tenant isolation, audit (fail-closed, payload-free) | fixture-tested; tamper evidence planned (M9) |
| Model and tool usage policies, token budgets | fixture-tested |
| Agent sandboxing (declared permissions, narrowed sub-agent tools) | fixture-tested |
| Observability: OTel traces, Prometheus metrics, usage/cost ledger | fixture-tested + verified in Jaeger |
| SSO/OIDC, approvals, redaction policies, retention jobs | planned (M9) |
