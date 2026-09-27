# UAR implementation plan

## Starting point and sequence

This is a new implementation in an empty workspace. The enhanced prompt is the proposed product specification; the decisions below are starting assumptions. No delivery estimate should be treated as a commitment until phase 0 confirms staffing, hardware, security requirements, and provider access.

The critical path is contracts and identity → inference → MCP execution → durable agents → simulation and local packaging → extensibility → cloud operations. Governance is part of every phase.

## Phase 0 — Resolve foundations

**Work:** record architecture decisions for the proposed Python stack, persistence, compact/distributed profiles, MCP revision, trust model, Google API surface, and SDK generation. Define API schemas, graph/plugin formats, role matrix, threat model, data retention, and version compatibility policy. Inventory target laptop resources and local model size. Choose measurable prototype performance targets before load testing.

**Deliverables:** OpenAPI and JSON Schemas, decision records, architecture diagram, capability matrix, prioritized backlog, resource budget, and test strategy.

**Exit gate:** a sample inference, report-agent run, tool call, policy denial, cancellation, and dry-run can all be represented unambiguously by the contracts. No unresolved decision blocks the MVP slice.

## Phase 1 — Runtime skeleton and governance foundation

**Work:** create the repository structure, build pipeline, local PostgreSQL environment, configuration validation, migrations, health/readiness checks, authentication interface, tenant-scoped access layer, RBAC, correlation IDs, audit writer, and structured errors. Add bounded request sizes and basic rate/concurrency limits. Implement persistence primitives for jobs and events without building the entire engine yet.

**Dependencies:** phase 0 contracts and trust model.

**Exit gate:** a clean checkout starts successfully; migrations work on an empty database; unauthorized operations fail; one tenant cannot read another tenant's records; audit and error redaction tests pass. Sensitive operations cannot proceed when mandatory audit writes fail.

## Phase 2 — First useful inference slice

**Work:** implement `POST /inference`, model aliases/catalog, Ollama and OpenAI adapters, streaming, deadlines, cancellation, capability rejection, usage normalization, and bounded concurrency. Start Node/Python clients against the contract. Keep cloud calls opt-in.

**Dependencies:** phase 1 identity and policy.

**Exit gate:** an example app uses either SDK to obtain a real local response and consume a stream. A deterministic adapter suite verifies errors and timeouts. The OpenAI adapter passes contract tests; record live verification separately when credentials are available. Unapproved cloud fallback is denied. Interrupting a stream does not trigger a second generation.

## Phase 3 — Governed MCP execution

**Work:** add administrator-managed server registry, stdio/Streamable HTTP lifecycle, discovery cache, input validation, authorization, timeouts, audit intent/completion, and result normalization. Integrate filesystem sandbox and read-only database servers. Classify tools by side-effect and retry behavior.

**Dependencies:** phase 1 security and phase 0 MCP choice.

**Exit gate:** real MCP servers can read a fixture file and query a fixture database through the unified API. Invalid arguments, forbidden roots, symlink escapes, unauthorized writes, unapproved endpoints, and cross-tenant credentials are rejected. A server failure yields a bounded, actionable error.

## Phase 4 — Durable agent engine

**Work:** implement graph registration/compilation; model, tool, transform, condition, bounded-loop, and return nodes; durable state; leased jobs; fencing; per-node checkpoints; run status/events; cancellation; retry rules; and ambiguous-write handling. Bind idempotency keys and version snapshots.

**Dependencies:** phases 2–3.

**Exit gate:** the report agent queries fixture data, generates a report using a real local model, and writes it to an authorized output directory. Killing a worker between nodes resumes from the checkpoint. Killing it after a write but before acknowledgment does not blindly repeat the write. Step/time budgets stop runaway loops; duplicate requests do not create duplicate runs.

## Phase 5 — Dry-run, packaging, and MVP release

**Work:** implement static planning and deterministic fixture simulation through a non-executing backend. Add explicit uncertainty and price-version metadata. Finish Node/Python wrappers, local Helm chart, persistent storage, optional ingress, image-loading instructions, Windows-friendly setup, troubleshooting, and demo fixtures.

**Dependencies:** phase 4 semantics and contracts.

**Exit gate:** a fresh Docker Desktop Kubernetes environment runs local inference, report generation, and dry-run without cloud credentials. Simulation succeeds with external execution disabled and instrumented tool/model entry points untouched; missing fixtures are reported as unresolved. Node/Python pass shared API fixtures. Helm install, upgrade, restart, and removal behavior are documented; persistent-data retention is explicit.

**MVP release artifacts:** tagged source, pinned container images, Helm chart, OpenAPI, two SDK distributions, runnable examples, test report, supported-capability matrix, and known limitations. Validate packages locally; registry publication is a separate authorized action.

## Phase 6 — Providers, plugins, and beta

**Work:** add LM Studio, llama.cpp, Anthropic, Groq, Azure OpenAI, and Google Vertex AI adapters; capability-aware policy routing; declarative and isolated executable plugins; version activation/rollback; approvals; browser MCP; optional scoped persistent memory; OIDC; and broader tenant isolation checks. Build Java/.NET/Go/Rust SDKs from the stabilized contract.

**Dependencies:** MVP contract and execution semantics.

**Exit gate:** each adapter has a documented compatibility/conformance result; live tests are recorded where access is available. Plugin upgrades preserve in-flight version pins and rollback works. Approval decisions cannot be replayed against changed arguments. Cross-tenant tests cover API, jobs, events, artifacts, credentials, and plugins. All six SDKs run inference, agent status/events/cancellation, and tool examples. Browser access remains within policy.

**Scope check:** implement the code-assistant and enterprise-workflow examples here. The enterprise example must demonstrate both approved and denied actions, with an audit trail. Do not label unverified providers production-ready.

## Phase 7 — Distributed deployment and operational readiness

**Work:** package independent service roles, add internal service authentication, TLS, enforced network policy, cloud secret integration, durable artifact storage, backups, tamper-evident audit export, metrics/traces, queue-aware autoscaling, and recovery procedures. Run the distributed profile locally before deploying to an authorized cloud environment. Select one cloud and prepare a cost estimate before provisioning.

**Dependencies:** beta interfaces and verified tenant boundaries.

**Exit gate:** a multi-worker deployment survives worker termination and controlled dependency failures without losing acknowledged jobs. Backup restoration recovers the documented state and event history. Authorization still applies across service calls. Load tests meet phase 0 targets; record deviations and capacity limits. Deployment, upgrade, rollback, key rotation, retention, incident response, and recovery runbooks are exercised.

Set availability, recovery-time, and recovery-point targets with the intended operator before this gate; do not invent service-level commitments from laptop benchmarks.

## Suggested repository structure

```text
contracts/              OpenAPI, graph/plugin schemas, shared fixtures
runtime/uar/            gateway, engine, router, mcp, plugins, governance
runtime/tests/          unit, contract, integration, recovery, security
migrations/             versioned database changes
sdks/                   node, python, java, dotnet, go, rust
deploy/                 Dockerfiles, Helm, local/distributed values
examples/               assistant, reports, code assistant, enterprise
tests/e2e/              black-box workflow and deployment tests
tests/load/             reproducible benchmarks and reports
docs/adr/               architecture decisions
docs/runbooks/          installation, upgrades, recovery, operations
```

## First implementation backlog

| Order | Work item | Completion evidence |
| --- | --- | --- |
| 1 | Record stack, trust model, and deployment decisions | Reviewed decision records with MVP constraints |
| 2 | Define inference/tool/run/dry-run schemas | Valid and invalid examples checked against schemas |
| 3 | Define graph limits and run-state transitions | Recovery and cancellation scenarios represented |
| 4 | Bootstrap service, database, configuration, CI | Clean startup and automated baseline checks |
| 5 | Add tenant identity, permissions, and audit | Denial, isolation, and redaction checks |
| 6 | Deliver Ollama inference through the gateway | Real local inference and streaming smoke test |
| 7 | Add provider fake and OpenAI adapter | Contract/error/timeout tests; live status documented |
| 8 | Add first Node/Python client example | Same fixture contract passes in both languages |
| 9 | Deliver one authorized filesystem MCP read | Success plus traversal/permission denials |
| 10 | Implement the minimal persisted agent path | Local model → authorized file output, restart-safe |

## Risks and decision triggers

| Risk | Treatment or trigger |
| --- | --- |
| Laptop cannot host models and all components | Measure early; compact profile, small configurable model, optional host inference endpoint |
| External write completed before worker crash | Idempotency/reconciliation where supported; otherwise explicit `needs_attention` |
| Provider differences hidden by normalization | Capability matrix, explicit unsupported-feature errors, per-adapter tests |
| Plugins receive excessive authority | Approved artifacts, declared capabilities, process/container isolation, restricted egress |
| Simulation appears more certain than reality | Typed fixtures, unresolved branches, estimated ranges, no real execution |
| Six SDKs drift | One contract, generated foundations, shared black-box fixtures |
| Queue contention or service bottlenecks | Benchmark before adding a broker or splitting additional processes |
| Cloud access or credentials unavailable | Complete fixtures/local delivery and clearly mark the remaining live gates unverified |

## Definition of completion

MVP completion means phases 0–5 pass their local gates. Full original-scope completion means phases 0–7 pass, all eight provider adapters and six SDKs are delivered with their verification status disclosed, all four examples run, and at least one cloud deployment has been validated. An unavailable external dependency leaves its live gate open; it does not invalidate completed local work or justify claiming the full platform is complete.
