# MVP exit gates (M0–M6): results

Recorded 2026-09-26 on the development machine: Windows 11, Python 3.14.6, Node 24.18.1, Docker
29.7.2, PostgreSQL 16 (container), NVIDIA RTX 5090 32 GB. Live models: Ollama `granite4:latest`
(3.4B), LM Studio `qwen/qwen3.8-27b` (JIT-loaded, unloads after 120 s idle).

**Legend:** ✅ passed with the evidence shown · 🟡 implemented, gate partly verified · ⛔ not verified here

Automated evidence: `pytest` → **103 passed** (default suite, ~20 s); `pytest -m live` → **5 passed,
1 skipped**; `pytest -m docker` → **1 passed**; Python SDK vs `uar-mock` **10 passed**; TypeScript
SDK vs `uar-mock` **9 passed**; both SDKs vs the live runtime pass inside the default suite.
Mutation check: disabling lease fencing, fail-closed audit, or the dry-run import boundary each
makes its guarding test fail (then restored).

## M0 — Foundations and contracts ✅

| Gate item | Evidence |
|---|---|
| Inference, streamed tool-calling inference, report-agent run, policy denial, cancellation, dry-run expressible in the contract | `proto/uarpb/v1/runtime.proto`; `contracts/fixtures/*.json` validate against the generated schema (`test_contracts::test_conformance_fixtures_match_contract`); approval is in the contract and returns 501 until M9 |
| Generated artifacts current; breaking-change check | `gen_contracts.py --check`, `check_breaking.py` (baseline `contracts/baseline/descriptor.pb`); detector proven by `test_breaking_change_detector_detects` |
| Graph / plugin / router schemas with valid and invalid examples | `contracts/schemas/agent.schema.json`, `test_invalid_graphs_rejected` (9 cases), `test_schema_rejects_invalid_examples`; plugin contract `proto/uarpb/plugin/v1/plugin.proto`; router/config schema = the pydantic models in `config.py` |
| ADRs, threat model, error catalogue | `docs/adr/0001-foundation-decisions.md`, `docs/threat-model.md`, `contracts/errors.md` |
| MCP revision confirmed | SDK 2.2.0 negotiates `2026-07-28`, falls back to `2025-11-25` (ADR 5) |
| Performance targets | ⛔ not set — needs the product owner (plan §11); baseline is an M10 task |

## M1 — Runtime core and governance base ✅

| Gate item | Evidence |
|---|---|
| Clean checkout starts healthy; migrations on an empty DB | Every test session creates an empty database and migrates it; `test_startup_and_health`; container stack `readyz` = ready |
| 401/403 on all three transports | `test_unauthenticated_rejected_on_all_transports` (HTTP, gRPC trailer, WebSocket), `test_rbac_viewer_cannot_infer_or_execute` |
| Cross-tenant reads/cancels denied | `test_cross_tenant_run_access_denied` (404 indistinguishable from missing) |
| Audit failure blocks the sensitive action | `test_audit_failure_blocks_sensitive_action` (file not written, 503 `audit_unavailable`); audit rows carry no payload (`test_audit_rows_written_without_payloads`) |
| Same fixture passes over gRPC and HTTP | `test_inference_identical_across_transports` (HTTP, gRPC, WebSocket identical) |
| Limits | `test_request_limits` (413, unknown field 400), `test_rate_limit_returns_429`; JWT tenant binding `test_jwt_bearer_and_tenant_binding` |

## M2 — Model router 🟡 (live llama.cpp pending its key; cloud never live-tested)

| Gate item | Evidence |
|---|---|
| Live local inference, sync + stream | ✅ Ollama `test_ollama_sync_and_stream`, structured output `test_ollama_structured_output`; ✅ LM Studio `test_lmstudio_sync_and_stream`; ⛔ **llama.cpp**: adapter works against the server's catalog, but the running `llama-server` requires an API key that was not available — set `UAR_LLAMACPP_KEY` and run `pytest -m live` |
| Cloud adapters pass contract tests | ✅ `test_adapters.py` (9 tests): Anthropic (official SDK), OpenAI Responses, OpenAI-compatible (Groq/vLLM) and Ollama against a local server replaying each API's **documented** wire format (sync JSON, SSE/NDJSON streams, tool calls, refusals, usage, 401/404/429 mapping, missing keys, connection refused). These are documentation-shaped fixtures, not captures of live traffic. ⛔ No live cloud call was made (no credentials) |
| Confidential → cloud denied; fallback only when allowed | `test_policy_denials`, `test_cloud_fallback_requires_tenant_opt_in`, `test_fallback_on_provider_unavailable` |
| Stream interruption never regenerates | `test_stream_disconnect_does_not_regenerate` |
| Capabilities, breakers, budgets, usage | `test_capability_rejection_and_unknown_model`, `test_circuit_breaker_opens_and_recovers`, `test_daily_token_budget_is_atomic` (concurrent reservations never exceed the limit), `test_usage_ledger_and_metrics` |

## M3 — MCP orchestrator and sandbox ✅

| Gate item | Evidence |
|---|---|
| Fixture file read and DB query through the API | `test_read_and_list`, `test_database_read_only` |
| Traversal, symlink/junction escape, forbidden root, unauthorized write rejected | `test_traversal_rejected` (8 paths incl. drive letters, ADS `:`, NUL), `test_link_escape_rejected` (Windows junction), `test_write_policy_and_exclusive_create` |
| Writes: policy + intent + never overwrite | same test; intents recorded `completed`/`failed` |
| Unregistered endpoint / egress | Clients cannot supply servers at all (config only); SSRF guard `test_ssrf_guard`, `test_streamable_http_transport` (denied without allow-list) |
| Server failure → bounded error, recovery | `test_server_crash_recovers`; argument validation `test_argument_schema_validation`; output cap `test_tool_output_is_capped` |
| Container sandbox | `test_container_sandbox`: no network, read-only rootfs, caps dropped, non-root, 256 MiB, verified via `docker inspect` |
| Streamable HTTP transport | `test_streamable_http_transport` |

## M4 — Agent engine ✅

| Gate item | Evidence |
|---|---|
| Report agent: DB → local model → authorized write | Fake model: `test_report_generator_end_to_end`; **real model**: `test_report_generator_with_real_model` (granite4) and via the container stack |
| Kill between nodes resumes without repeats | `test_resume_between_nodes_does_not_repeat` |
| Kill after write: no blind repeat | `test_crash_after_write_reuses_recorded_outcome`; open intent → `test_ambiguous_write_needs_attention_then_resolve` (operator `retry_node`, developer lacks `runs:resolve`) |
| Stale worker cannot commit (fencing) | `test_stale_worker_cannot_commit` (checkpoint and event paths each fenced) |
| Loops / limits / duplicates | `test_loop_condition_and_limits`, `test_idempotent_run_start`, `test_input_schema_enforced` |
| Sub-agent cannot exceed parent | `test_subagent_cannot_exceed_parent_permissions` |
| Cancellation interrupts a running node | `test_cancellation_of_running_node` (< 5 s), idempotent |
| Parallel nodes; writes refused in parallel | `test_parallel_node_and_write_restriction` |
| Revoked credential stops the run | `test_revoked_credential_stops_run` |

## M5 — Dry-run and observability ✅

| Gate item | Evidence |
|---|---|
| Execution entry points instrumented to fail; dry-run still succeeds | `test_static_plan_executes_nothing`, `test_simulation_with_fixtures` (tripwires on adapters, MCP calls/sessions, subprocess, httpx) + import-graph test `test_simulation_import_graph_is_execution_free` |
| Missing fixtures reported, not invented | `test_missing_fixture_is_unresolved_not_invented`; unknown prices → no cost, listed as unresolved |
| One agent run = one connected trace | `test_agent_run_is_one_connected_trace` (in-memory exporter) and Jaeger in the container stack: HTTP → agent.run → nodes → model.chat / tool.execute → MCP client span |
| Metrics equal the ledger | `test_metrics_match_ledger` |

## M6 — MVP packaging and SDKs 🟡

| Gate item | Evidence |
|---|---|
| Python + TypeScript SDKs pass shared fixtures | ✅ against `uar-mock` and the live runtime (`test_sdk_live.py`); original README snippets run unchanged (`examples/python/quickstart.py`, `examples/typescript/quickstart.mjs`) |
| Containerised stack without cloud credentials | ✅ `docker compose --profile full`: migrations, provider discovery (Ollama 8, LM Studio 3, llama.cpp 7 models via `host.docker.internal`), MCP 3+2 tools, agents registered; inference, agent run, streaming and dry-run from both SDKs |
| **Fresh Docker Desktop Kubernetes install; Helm install/upgrade/rollback/uninstall** | ⛔ **Not verified.** Kubernetes is not enabled in Docker Desktop on this machine and Helm is not installed. The chart (`deploy/helm/uar`) is written but has not been linted or rendered. CI includes a `helm lint`/`template` job that has not run (no remote). |
| End-to-end smoke against a running deployment | ✅ `scripts/smoke_test.py`: 27/27 checks (HTTP, SSE, gRPC, WebSocket, RBAC, MCP, agents, dry-run, metrics) against the Compose stack with real Ollama, 2026-09-27. It found a packaging bug the in-process suite could not: `websockets` was missing from `requirements.lock`, so the image answered WebSocket upgrades with 404. Fixed and re-verified |
| Release artifacts built locally | ✅ image `uar-runtime:0.6.0`, `requirements.lock`, OpenAPI/JSON Schema, SDK sources and TS build; packages not published |

## Open items before the MVP gate can be declared fully passed

1. Enable Kubernetes in Docker Desktop, install Helm, then follow `docs/runbooks/docker-desktop.md`
   (install, upgrade, rollback, uninstall, PVC retention).
2. Run the llama.cpp live gate with its API key.
3. Live cloud runs (opt-in, needs credentials and spend approval): Anthropic, OpenAI, Groq.
4. Agree performance targets (then baseline in M10).
