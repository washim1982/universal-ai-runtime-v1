# Master implementation prompt — Universal AI Runtime

## Objective

Build Universal AI Runtime (UAR), a self-hostable platform that gives existing applications one versioned API for model inference, durable agent workflows, and governed MCP tool execution. Deliver a working local prototype on Docker Desktop Kubernetes, then extend the same contracts to a cloud-ready multi-service deployment.

Preserve the full product scope: local and cloud models; YAML/JSON agent graphs; plugins for models, tools, agents, and workflows; offline dry-run simulation; governance and auditing; and SDKs for Node.js/TypeScript, Python, Java, .NET, Go, and Rust.

Do not describe unfinished integrations as supported. Distinguish implemented, fixture-tested, live-tested, experimental, and planned capabilities in documentation.

## Delivery boundaries

**MVP:** one tenant with enforced identity and scoped permissions, Ollama plus an OpenAI adapter, inference with streaming, durable sequential/branching/bounded-loop agents, filesystem and read-only database MCP tools, offline simulation, audit records, Node/Python SDKs, and local Helm deployment. Cloud credentials are optional; local acceptance must work without them.

**Beta:** remaining providers, browser tooling, governed approvals, executable plugins, richer routing and memory, all six SDKs, and verified multi-tenant isolation.

**Cloud release:** separate services, horizontal worker scaling, hardened networking and identity, backup/restore, observability, operational documentation, and deployment validation on one selected cloud. Other clouds remain documented targets until tested.

Exclude model training, billing/payment processing, a public plugin marketplace, arbitrary code execution inside graph expressions, active-active multi-region operation, and a graphical workflow editor from the initial release. Embeddings, multimodal inference, and parallel graph nodes require separate contracts before inclusion.

## Proposed implementation baseline

- Python/FastAPI with typed validation for the runtime; asynchronous I/O for provider and MCP calls.
- PostgreSQL for tenant-scoped configuration, jobs, checkpoints, run events, approvals, and audit records. Begin with a leased PostgreSQL job queue; do not require another broker before measurements justify it.
- OpenAPI as the HTTP contract, JSON Schema for graph/plugin formats, SSE for streaming, structured errors, and OpenTelemetry-compatible instrumentation.
- One monorepo; shared domain contracts with explicit module interfaces. A compact profile may colocate first-party gateway, engine, router, and MCP client code. Untrusted tools and plugins stay outside the API process.
- Docker images and Helm profiles for `local` and `distributed`. Pin dependencies and image versions; commit lockfiles and migrations.

Record alternatives and rationale in architecture decision records. Confirm supported dependency versions during implementation against primary documentation.

## Architecture and ownership

| Component | Responsibility |
| --- | --- |
| API gateway | Authentication, quotas, validation, idempotency, request correlation, HTTP/SSE contract |
| Agent engine | Graph compilation, scheduling, bounded execution, checkpoints, recovery, cancellation |
| Model router | Alias resolution, capability checks, policy-compliant selection, provider adapters, usage accounting |
| MCP orchestrator | Connection/session lifecycle, tool catalog, validated execution, timeouts, authorization |
| Plugin runtime | Manifest validation, immutable versions, isolated workers, activation and rollback |
| Governance | Policy decisions, approvals, tenant boundaries, redaction, audit |
| Persistence | Durable jobs, runs, events, configuration versions, secrets references |

Use `uar-system` for core workloads and `uar-plugins` for tool/plugin workloads. Namespaces alone do not provide tenant isolation. Enforce identity and tenant scope at API, storage, queue, cache, artifact, and tool boundaries.

Workers lease jobs and heartbeat while processing. Use fencing/version checks to prevent stale workers from committing after lease loss. Persist checkpoint changes and emitted durable events atomically. When splitting services, each module owns its tables and mutations; use explicit interfaces for cross-module access.

## Public API requirements

Retain the original endpoints and add lifecycle operations:

| Endpoint | Behavior |
| --- | --- |
| `POST /api/v1/inference` | Synchronous inference or SSE when `stream: true` |
| `POST /api/v1/agent/run` | Accept a versioned agent plus input; return `202` and `run_id` |
| `POST /api/v1/tool/execute` | Validate and execute an authorized tool; return result or an async run reference |
| `POST /api/v1/dry-run` | Offline validation, routing preview, fixture simulation, and estimated usage |
| `GET /api/v1/runs/{run_id}` | Run status, result, safe error details, usage |
| `GET /api/v1/runs/{run_id}/events` | Ordered SSE events with durable event IDs and resumable run-event delivery |
| `POST /api/v1/runs/{run_id}/cancel` | Idempotent cancellation request |
| `POST /api/v1/agents` | Validate and register an immutable graph version |
| `GET /api/v1/models` | Configured aliases and capabilities visible to the caller |
| `GET /api/v1/tools` | Authorized registered tool catalog |
| `POST /api/v1/mcp/servers` | Administrator-only registration of an approved server configuration |
| `POST /api/v1/plugins` | Administrator-only registration and validation of a plugin version |
| `POST /api/v1/plugins/{plugin_id}/activate` | Activate a validated version; support rollback to a prior version |
| `POST /api/v1/approvals/{approval_id}/decision` | Authorized, audited approval or rejection bound to exact action arguments |

Define request/response schemas, limits, pagination, error codes, and authorization for every operation before implementing it. Errors include `code`, `message`, `request_id`, `retryable`, and redacted details. Use standard HTTP status semantics, including `429` for quotas and `503` for unavailable capacity.

Inference requests use `model`, `messages`, optional generation settings, and `stream`; responses expose the resolved provider/model, content, finish reason, usage, and request ID. Reject unsupported capabilities explicitly. Provider-specific extensions must be namespaced.

Scope idempotency keys by tenant, operation, and normalized request hash; reject reuse with different inputs. Document retention and concurrency behavior. Request-level idempotency does not guarantee exactly-once external effects.

Inference streams emit start, delta, usage when available, and one terminal success/error event. Do not transparently restart a partially emitted generation. Run-event streams support replay by event ID; an interrupted direct inference stream is not resumable unless separately implemented and documented.

## Agent graph and execution semantics

Graphs have a schema version, immutable agent version, typed input/output, declared permissions, state schema, nodes, transitions, and execution limits. Initially support model, tool, transform, condition, loop, and return nodes. Add approval nodes in beta. Validate missing references, unreachable nodes, invalid transitions, and cycles outside explicit loop constructs before registration.

Use a restricted expression language with no arbitrary Python/JavaScript evaluation, file access, or network access. Enforce maximum steps, loop iterations, elapsed time, token use, tool calls, and estimated spend. Defaults must be bounded and configurable by administrators.

Run states: `queued`, `running`, `waiting_approval`, `succeeded`, `failed`, `cancelled`, and `needs_attention`. Persist node inputs/outputs subject to redaction and retention policies, attempt counts, route decisions, and checkpoints. Pin graph, policy, and plugin versions to a run. Snapshot relevant configuration; recheck current revocations before each sensitive action.

Retry transient read-only failures with capped backoff and jitter. Never blindly retry a potentially completed write. Record tool intent before dispatch and completion afterward. On an ambiguous outcome, use a downstream idempotency key or reconciliation if supported; otherwise mark the run `needs_attention` and require an explicit recovery decision.

Cancellation stops scheduling new nodes and attempts to cancel in-flight requests. It cannot promise reversal of completed external actions. Durable restart resumes from checkpoints without repeating completed nodes. Start with run-scoped memory; persistent cross-run memory needs tenant/agent scope, retention, size limits, and explicit opt-in.

## Model router

Implement adapters for Ollama, LM Studio, llama.cpp, OpenAI, Anthropic, Groq, Azure OpenAI, and Google Vertex AI; document the selected Google surface and add other Google APIs separately if required. Alias names such as `local:default` and `cloud:default` map to configured provider/model IDs; examples must not imply a permanently available model catalog.

Adapters normalize messages, streaming, usage, errors, cancellation, timeouts, and capabilities. Publish a capability matrix and conformance tests. Route only to destinations allowed by tenant policy, data classification, region requirements, and budget. Local-to-cloud fallback is disabled unless explicitly permitted. Apply concurrency limits and circuit breakers; fallback must not silently change required capabilities or bypass governance.

Keep prices in a versioned catalog with currency, unit, and update timestamp. Distinguish estimated from provider-reported usage. Unknown prices remain unknown rather than zero. Budget reservations must be atomic across concurrent requests; disclose that provider-side usage and cancellation delays prevent absolute billing guarantees.

## MCP tools and plugins

Pin an MCP protocol revision and compatible official SDK. Support negotiated initialization, tool discovery, JSON Schema input validation, result/error normalization, connection recovery, and timeouts. Support supervised stdio for local processes and Streamable HTTP for remote servers. Handle transport-specific authentication without forwarding unrelated user credentials.

Servers are administrator-registered and allowlisted. Do not accept arbitrary client-provided endpoints or commands. Apply egress restrictions and guard against redirects/DNS resolution reaching forbidden destinations. Authorize every tool invocation using caller identity, tenant, declared capability, exact arguments, and policy. Tool descriptions and returned content are untrusted data.

Filesystem tools operate within explicit roots and resist traversal and symlink escapes. Database tools use scoped credentials, parameterized operations, row/time limits, and read-only access by default. Browser tools use isolated contexts, URL policies, and bounded downloads. Writes or external communications require explicit policy authorization and, when configured, a durable approval bound to action and argument hashes.

Plugin manifests contain identity, immutable version, runtime/API compatibility, artifact digest, entry point, requested capabilities, input/output schemas, configuration schema, resource limits, and secret references. Declarative agent plugins can ship first. Executable plugins run in isolated workers/containers with resource limits and restricted networking; do not dynamically import third-party code into the gateway.

Activation requires validation and health checks. Pin active runs to their selected version; rollback affects new runs. Use approved registries/artifacts and verify integrity. Containers alone are not a sufficient sandbox for actively hostile code; exclude hostile public submissions from the initial supported trust model.

## Dry-run guarantees

Provide two explicit modes: static plan and deterministic fixture simulation. Neither calls models, executes tools, starts plugin entry points, probes servers, or performs live discovery. Use cached versioned capabilities and fixtures; mark unavailable information as unresolved.

Static planning validates graphs, evaluates resolvable policy/routing decisions, enumerates branches, and reports permission needs, estimated ranges, and uncertainty. Fixture simulation follows a reproducible path with a seed and typed node responses. Unknown outputs do not become fabricated successful results. Never claim previews predict model behavior exactly.

Do not persist simulated business output. Permit only the documented administrative audit record for the preview request. Verify the execution backend cannot be reached from simulation, including indirect plugin or network paths.

## Governance, deployment, and quality

Use an authentication interface: tenant-bound development credentials for local use, OIDC for production. Never trust a caller-supplied tenant ID without identity binding. Enforce runtime RBAC separately from Kubernetes RBAC; use least-privilege service accounts. Store secret references in manifests, and redact credentials and sensitive content from logs and errors.

Audit privileged decisions and external action attempts with actor, tenant, run, timestamps, target, outcome, policy version, and correlation ID. Sensitive execution fails closed if the required audit intent cannot be recorded. Production audit storage must support tamper evidence and restricted append access; define retention and export procedures.

The local Helm profile includes probes, resource requests/limits, persistent volumes, migrations, and documented image loading. Ollama is the default local model backend; LM Studio may run on the host or use a separately validated headless deployment. Make host connectivity explicit. Default to port forwarding; ingress requires a configured controller. Do not assume GPUs, unlimited laptop memory, or cloud free-tier availability.

The distributed profile deploys gateway, engine workers, router, orchestrator, and plugin workers separately with internal Services where needed. Add TLS, service identity, enforced network policy, secret management, backups, bounded queues, autoscaling, and graceful shutdown. Verify the chosen CNI actually enforces network policy.

Generate SDK foundations from OpenAPI and add ergonomic streaming, retries, cancellation, and typed-error wrappers. Deliver Node/Python first; Java/.NET/Go/Rust are required for full scope completion. Never automatically retry non-idempotent writes. Use identical contract fixtures across SDKs. Package names are placeholders until availability and publication rights are checked.

Tests must cover contract compatibility, invalid graphs, bounded loops, policy denials, tenant isolation, malicious tool output, stream interruption, timeout, worker restart, ambiguous writes, plugin rollback, and dry-run non-execution. Use deterministic fakes for CI and opt-in live-provider suites. Publish benchmark hardware, model configuration, concurrency, error rate, queue delay, and latency percentiles; separate runtime overhead from model generation time.

## Required delivery behavior

Implement one vertical slice at a time using the companion implementation plan. Each phase produces runnable code, updated contracts, migrations where necessary, meaningful tests, example requests, and a documented exit-gate result. Provide setup and troubleshooting instructions that a clean machine can follow. Do not mark a phase complete on the basis of mocks when its exit gate requires real execution.

Finish with four documented use cases: in-app assistant, report generator, code assistant, and enterprise workflow. Each must identify required permissions, fixtures, outputs, and failure behavior. Do not publish packages, incur cloud spending, or deploy to an external account without authorization.
