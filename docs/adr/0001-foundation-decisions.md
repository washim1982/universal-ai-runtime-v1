# ADR 0001–0014: Foundation decisions (M0)

Status: **Accepted** 2026-09-26. Each entry: decision, rationale, consequences. Deviations from the
plan are marked **Changed**.

| # | Decision | Rationale | Consequences |
|---|---|---|---|
| 1 | Runtime in Python 3.12+ asyncio: FastAPI (HTTP/SSE/WebSocket), grpcio aio (gRPC) | Official MCP SDK and provider SDKs; I/O-bound workload | Verified on 3.14 (Windows host, Linux image). Hot paths can move to Go behind the proto. |
| 2 | Protobuf is canonical; OpenAPI, JSON Schema and TS types are generated | "One protocol"; free gRPC stubs for Java/.NET/Go/Rust | `scripts/gen_contracts.py --check` and `gen_ts_types.py --check` in CI. **Changed:** protos live under `proto/uarpb/` (wire package stays `uar.v1`) so generated Python stubs import as `uarpb`, leaving `uar` to the Python SDK. |
| 3 | Three transports on one service layer | Meets gRPC + JSON + WebSocket requirement without duplicated logic | Every response is parsed into its proto message before it is sent (`gateway/codec.py`), so no transport can drift from the contract. |
| 4 | Plugin protocol: gRPC `uar.plugin.v1.Plugin`, out of process only | Any-language plugins, isolation | Contract only in M0; supervisor is M7. |
| 5 | MCP: official Python SDK 2.2.0; stdio (process or container) and Streamable HTTP | Ecosystem standard | **Changed:** the SDK negotiates protocol revision `2026-07-28` via `server/discover` and falls back to the `2025-11-25` initialize handshake (`mode="auto"`); the plan pinned 2025-11-25 only. Both are exercised by first-party servers. |
| 6 | PostgreSQL for config, runs, checkpoints, events, audit, jobs; leased queue with `FOR UPDATE SKIP LOCKED` | One stateful dependency; transactional checkpoints | No broker. Revisit after M10 load tests. |
| 7 | CEL (`cel-python`) inside `${ }` for mappings and guards; `{{ }}` templates for prompts | Sandboxed, non-Turing-complete | celpy has no static type checker: registration checks syntax and node references; type errors surface at run time as node failures. |
| 8 | Built-in YAML policies (router rules, tool policies, RBAC); default-deny for tools | No extra infrastructure | `governance.tool_decision` behind a narrow interface; OPA/Cedar adapter possible later. |
| 9 | OpenTelemetry + Prometheus + JSON logs | Vendor-neutral | Run trace context persisted as W3C `traceparent`; verified in Jaeger. |
| 10 | One OpenAI-compatible adapter for LM Studio, llama.cpp, Groq, vLLM; native Ollama, OpenAI Responses, Anthropic | Halves adapter count | **Changed:** the Anthropic adapter uses the official `anthropic` SDK (1.8.0) rather than raw HTTP, and enables server-side refusal fallbacks (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`) for `claude-opus-5` / `claude-fable-5-1`; disable per request with provider extension `{"anthropic": {"fallbacks": "off"}}`. Sampling parameters are not sent to Anthropic (current models reject them). |
| 11 | Tools/plugins in Linux containers for isolation; `--network none --read-only --cap-drop ALL --user 10001 --memory --pids-limit` | Real isolation on Windows via Docker Desktop | Container mode verified; the default dev config uses process mode for speed. gVisor is optional (M9). |
| 12 | SDKs: generated types + thin ergonomic layer + shared conformance fixtures + `uar-mock` | Prevents drift | Python and TS pass the same 8 fixtures against the mock **and** the live runtime. |
| 13 | **New:** Durable intent protocol for write/external tools | Never blindly repeat a write | Checkpoint `pending` → intent row → call → completion (with outcome). Open intent after a crash or timeout ⇒ `needs_attention`; operator resolves (`mark_completed`, `retry_node`, `fail`). |
| 14 | **New:** Windows event loop | psycopg async requires a selector loop; MCP stdio falls back to a Popen-backed process on it | `store.selector_loop_factory()` used by the CLI and tests. |

## Deferred decisions (need the product owner)

Team size and dates; M10 target cloud; first `enterprise:` endpoints; whether WebSocket stays in
the MVP (it is implemented); whether the earlier prototype is recovered (not needed any more).
