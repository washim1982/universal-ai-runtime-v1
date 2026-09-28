# Post-MVP exit gates (M7–M10): results

Same machine and legend as [mvp-gates.md](mvp-gates.md): ✅ passed with the evidence shown ·
🟡 implemented, gate partly verified · ⛔ not verified here.

## M7 — Plugin system and remaining providers ✅

Recorded 2026-09-27. Default suite: **118 passed** (adds `test_plugins.py`, 12 tests, and 3 adapter tests).

| Gate item | Evidence |
|---|---|
| A third-party model provider, written in Go, serves `enterprise:acme/sentiment` with no gateway change | `test_go_model_plugin`: `plugins/reference/go-model` built with `plugin-sdks/go`, registered and activated through the API, answered through `/api/v1/inference` |
| Upgrading mid-run keeps the in-flight run on the old version; rollback serves new runs from the prior version | `test_upgrade_pins_running_runs_and_rollback`: each run records `plugins` at start; 1.0.0 run → "one", 2.0.0 run → "two", after rollback → "one" |
| A plugin asking for an undeclared capability is refused at activation | `test_undeclared_capability_refused` (status becomes `failed`, message recorded) |
| A hanging plugin is timed out and circuit-broken | `test_hanging_plugin_times_out_then_circuit_opens`: 3 × `deadline_exceeded`, then `unavailable` |
| Artifact integrity verified before activation | `test_artifact_integrity_checked` (wrong sha256 refused; correct one activates). Container plugins require the image id as digest |
| Immutable versions; administrators only | `test_versions_are_immutable`, `test_only_admins_manage_plugins` (new permission `plugins:manage`) |
| Tool plugins governed like MCP tools | `test_tool_plugin_is_governed_like_mcp_tools` (policy, schema validation from the descriptor, audit, `untrusted`, side effect from the manifest; undeclared tools default to `write`); TypeScript reference plugin `test_typescript_tool_plugin` |
| Agent plugins: executable and declarative | `test_agent_plugin_called_from_a_graph` (Python reference plugin called as a sub-agent); `test_declarative_agent_plugin_registers_graph_for_every_tenant` (`runtime.mode: none`) |
| Plugin SDK helpers | Python `plugin-sdks/python/uar_plugin`, Go `plugin-sdks/go`, TypeScript `plugin-sdks/typescript` — each used by a reference plugin that passes its test. Java/.NET helpers come with M8 |
| Azure OpenAI, Vertex AI adapters | `test_azure_openai_v1_and_classic_paths`, `test_vertex_generate_content_tools_and_schema`, `test_vertex_stream_safety_and_missing_credentials` against documented wire formats. ⛔ Not live-tested (no credentials) |
| LM Studio / llama.cpp conformance | LM Studio live-tested (M6); llama.cpp chat still needs its API key |

Found and fixed while testing M7: with MCP SDK 2.2.0, the client's automatic probe for protocol
revision 2026-07-28 makes a stateless Streamable HTTP server drop the connection. HTTP MCP servers
now fall back to the initialize handshake once (per-server `protocol: auto | legacy`).

Limits of the M7 implementation, stated plainly:
- Container-mode plugins run with a read-only filesystem, no capabilities, non-root user and
  memory/pid limits, but reach the runtime through a published loopback port, so their egress is
  not restricted locally. In Kubernetes, NetworkPolicy provides that (M10).
- Plugin gRPC traffic is unauthenticated plaintext on loopback; mTLS between services is M10.
- Capabilities are declarative (network hosts, secret names). Secrets are only the ones declared.

## M8 — SDK wave 2 ✅

Recorded 2026-09-27. Toolchains: Go 1.27, Rust 1.98 (cargo), .NET SDK 10 (targets net8.0), JDK 21 + Maven 3.9.16.

| Gate item | Evidence |
|---|---|
| All six SDKs pass 100% of the shared conformance fixtures against `uar-mock` | Python 10, TypeScript 9, Go (`go test`: 8 fixtures + retry), Rust (`cargo test`: 8 fixtures + retry + doc example), .NET (console runner: 8 fixtures + retry), Java (JUnit: 10 tests) |
| …and against a live runtime | `runtime/tests/test_sdk_live.py`: **6 passed** (each SDK's own suite pointed at the live test runtime) |
| Idiomatic API, typed errors, cancellation, no automatic retry of non-idempotent calls | Go: context-first, `*Error` with status helpers; Rust: async `futures::Stream`, `Error::Api`; .NET: `IAsyncEnumerable<UarEvent>`, `UarException`; Java: `Stream<UarEvent>` (closeable), `UARException`. Each has a no-retry test |
| The original snippets run essentially unchanged | Java: `new UARClient(url)` / `client.inference(model, prompt, agent)` / `resp.getText()` — `examples/java/Quickstart.java` compiles against the SDK; Python and TypeScript examples ran in M6 |
| Java and .NET plugin SDK helpers | `plugin-sdks/java` (grpc-java, stubs generated from the proto at build time) with `plugins/reference/java-agent` → `test_java_agent_plugin`; `plugin-sdks/dotnet` (Grpc.AspNetCore) with `plugins/reference/dotnet-tool` → `test_dotnet_tool_plugin` |

Decision recorded in [ADR 0015](../adr/0015-sdk-transport.md): every SDK uses the HTTP/JSON + SSE surface
so one set of fixtures and one mock serve all six; gRPC stays available through the generated stubs.

Found and fixed: the JDK's `HttpClient` attempts a cleartext HTTP/2 upgrade (h2c) that drops request
bodies on the runtime's HTTP server, so the Java SDK pins HTTP/1.1. On this machine Java needs the
Windows trust store (`-Djavax.net.ssl.trustStoreType=Windows-ROOT`) because a TLS-inspecting
component's root certificate is trusted by Windows but not by the JDK.

## M9 — Enterprise governance 🟡

Recorded 2026-09-27. Default suite: **157 passed** (adds `test_governance.py` 27, `test_router_rules.py` 2,
`test_admin_api.py` 6). Migrations `0003_governance`, `0004_api_keys`.

| Gate item | Evidence |
|---|---|
| Approval nodes; approved and denied paths | `test_approval_node_approved_path_and_no_replay`, `test_approval_node_rejected_path` (`on_reject: continue` routes to the denied branch) |
| Inline approvals bound to action + argument hash, used once, no replay | `test_tool_call_waits_for_approval_of_its_exact_arguments`, `test_approval_consumed_once`, `test_other_tenant_runs_do_not_reuse_approvals`; a wrong `args_hash` on the decision is refused |
| Expiry fails closed | `test_unanswered_approval_expires_and_fails_closed` |
| Separation of duties, approver roles | requester cannot decide (`test_requester_cannot_approve_own_run`); approver without the named role → 403 |
| Approvals inside sub-agents; cancel while waiting | `test_sub_agent_approval_parks_and_resumes_parent`, `test_cancel_waiting_run_cancels_its_approval` |
| Tamper-evident audit, verify and export | `test_audit_chain_verifies_and_exports`, `test_audit_is_append_only_and_tampering_is_detected` (modified row, deleted row and truncated tail each detected; UPDATE/DELETE blocked by a trigger) |
| OIDC/JWKS SSO, group → role mapping, service accounts | `test_oidc_*` (valid, wrong audience, expired, unmapped tenant, foreign issuer, forged signature, HS256 algorithm confusion, key rotation) |
| Router rules v2 (region, residency, cost/latency) | `test_router_data_residency`, `test_router_region_cost_and_latency_rules` |
| Redaction policies | `test_prompts_to_cloud_models_are_redacted`, `test_event_log_and_audit_are_redacted` |
| Retention/deletion jobs | `test_retention_prunes_and_audit_chain_still_verifies` (chain anchored after pruning) |
| Cross-tenant suite incl. approvals, audit, keys, usage | `test_other_tenant_cannot_see_or_decide_approvals`, `test_api_key_lifecycle`, `test_inference_history_is_tenant_scoped_and_paged` |
| Application registration + STS tokens | `test_sts.py` (14): register, client credentials, token, inference; scope narrowing; forged, tampered, expired, `none`/HS256 tokens refused; secret rotation; instant disable; key rotation without downtime; encrypted keys; `require_tokens`; gRPC; Python and TypeScript SDKs; `uar register-app` |
| Administration API + Windows admin app | `test_admin_api.py`; UAR Admin (`admin-app/`) exercised against a live runtime (see its README) |

Still open for M9: the compliance controls map document and the two worked examples
(code assistant with approved/denied file writes, enterprise workflow). Approval of direct
`ExecuteTool` calls is not supported: tools that need approval run only inside agent runs.

## M10 — Distributed deployment and production readiness

Pending.
