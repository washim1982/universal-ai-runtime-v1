# Implementation status — initial local prototype

This build implements the first working UAR vertical slice. It does **not** complete all phases of the master plan or meet the full MVP release gate yet.

## Delivered and verified

| Area | Implemented behavior | Evidence |
| --- | --- | --- |
| API foundation | Versioned FastAPI routes, validation, structured errors, request IDs, bounded request bodies, health/readiness | Automated API tests; live local server |
| Identity/governance | Tenant-bound development credentials, exact permissions, audit intent/completion, actor quotas | Authorization, cross-tenant read/cancel/event denial, audit-failure and redaction tests |
| Persistence | PostgreSQL schema version 1, immutable agents, tenant-scoped run idempotency, checkpoints/events | Real PostgreSQL concurrent claim/idempotency test |
| Agent execution | Model/tool/transform/condition/return nodes, bounded loops, state references, deadlines, cancellation | Checkpoint recovery, stale-worker rejection, loop/step/deadline/cancellation tests |
| Write recovery | Intent recorded before tool dispatch; uncertain writes become `needs_attention` | Simulated crash/reclaim test proves no tool replay |
| Ollama | Real non-streaming and streaming chat inference, normalized usage | Live `granite4:latest` inference on the existing host server |
| OpenAI | Responses adapter with streaming parser, cloud permission check, environment-held key | Non-streaming contract/error tests; live API and OpenAI streaming still unverified |
| MCP stdio | Official SDK lifecycle, catalog and argument validation, timeouts, separate processes | Real subprocess tests on Windows and Linux |
| Local tools | Confined reads, exclusive-create report writes, fixed read-only sales database query | Real MCP tests and successful end-to-end report workflow |
| Dry-run | Static catalogs and deterministic fixture simulation, explicit unresolved output/unknown cost | Network and execution entry points disabled in tests |
| SDKs | Python async client; Node JavaScript client plus TypeScript declarations | Shared request/response fixtures, SSE decoding, typed-error/no-retry tests |
| Packaging | Dependency lock, Docker image, private configuration bootstrap, local Helm chart, CI workflow | Linux image builds; image-based tests; Helm lint/render passes |

Verification completed in this workspace:

- **28 Python tests passed:** 27 runtime/SDK/MCP checks plus one real PostgreSQL integration check.
- **3 Node tests passed.**
- **27 tests also passed inside the Linux image.**
- The live smoke script passed inference, streaming, MCP filesystem read, offline planning, and durable database-to-model-to-report generation.
- Helm 3.19.0 lint passed and six Kubernetes resources rendered. The optional ingress was not enabled.

The live demo generated reports under `.local/workspace/`. Credentials and local configuration are ignored by source control. No cloud API call, package publication, GitHub push, or external deployment was performed.

## Implemented but not fully validated

- Streamable HTTP MCP connection code needs a live authenticated remote-server integration test and egress enforcement.
- OpenAI streaming needs dedicated event-fixture coverage and an opt-in live check. No OpenAI credentials were used.
- The Helm chart has no live rollout/upgrade/PVC recovery result: no Kubernetes context is configured.
- Separate worker mode and fencing primitives exist; multi-process failure injection and sustained load tests remain open.
- CI configuration is present but has not run in a hosted repository. Python 3.14 on Windows/Linux is verified; other declared Python versions are not.

## Remaining before the planned MVP gate

1. Tighten response typing and error declarations across all exported API operations; generate SDK foundations from the OpenAPI source and add compatibility checks.
2. Validate tool/graph configurations at startup more comprehensively; add live Streamable HTTP coverage, administrator registration lifecycle, connection pooling/recovery, and classified read-only retry policy.
3. Complete cost/pricing catalogs, atomic budget reservations, fuller token accounting, circuit breakers, policy version management, and structured observability. Current token admission uses a conservative prompt-byte/output-cap reservation, not a billing guarantee.
4. Add retention/deletion controls for run state and audit, configurable content redaction, and durable management of tenant credentials. Audit currently omits payloads but is not tamper-evident or database-enforced append-only.
5. Expand simulation output/fixture schema coverage, capability validation, branch analysis, and budget parity with real execution. Missing prices remain unknown.
6. Exercise a clean Kubernetes install, model connectivity, upgrade/restart/storage retention, and recovery. Benchmark before choosing resource/SLO targets.
7. Test cancellation during actual writes, symlink/junction races, abrupt worker process termination, large streaming/backpressure workloads, and fault scenarios beyond the current deterministic recovery tests.

## Beta/cloud backlog retained

LM Studio, llama.cpp, Anthropic, Groq, Azure OpenAI, Google Vertex AI adapters; browser tools; isolated executable/declarative plugin activation and rollback; durable approvals; scoped cross-run memory; Java/.NET/Go/Rust SDKs; OIDC; production tenant isolation and egress policy; separated services; backup/restore; audit integrity; distributed quotas; autoscaling; and an authorized cloud deployment.

The code-assistant and enterprise-approval examples depend on this backlog. The implemented examples are the in-app assistant and report generator. No placeholder routes claim that the remaining capabilities work.

## Trust and operational boundaries

Use this build on a trusted local machine with operator-owned configuration and first-party tools. Arbitrary third-party process commands are not sandboxed by the runtime. Filesystem roots reject traversal and links, but parent-directory races against hostile local processes are not a supported threat model yet.

The default process serves one embedded worker, and quotas are per process. Run state can contain model/tool content. Keep the API on loopback until production identity, isolation, retention, and monitoring are implemented. The plan's cloud release and full original-scope completion gates remain open.
