# UAR master implementation plan (v2)

**Scope source:** every UAR ✔ in `Mission_Comparison.md`, the original UAR prompt, and the constraints in `ENHANCED-IMPLEMENTATION-PROMPT.md`, `IMPLEMENTATION-PLAN.md` and `PROMPT-REVIEW.md`.
**Supersedes:** `IMPLEMENTATION-PLAN.md`. Its phase gates are kept and extended.
**Starting point:** greenfield. `IMPLEMENTATION-STATUS.md` describes a Python prototype, but that code is not in this workspace. If it is recovered, re-baseline milestones M1–M6 against it; don't rebuild.

---

## 1. Goal

UAR is an embeddable **runtime + execution engine + SDK layer**. Any application (Python, TypeScript, Java, .NET, Go, Rust) can call one protocol to:

1. run **inference** on local, cloud or enterprise models through a policy-driven router;
2. execute **MCP tools**, native tool plugins included, under governance and sandboxing;
3. run **declarative agent graphs** (YAML/JSON) durably, with sub-agents, loops, approvals and memory;
4. **dry-run** any of the above without executing anything;
5. **observe** tokens, latency, cost, tool calls and agent steps.

A ✔ in the comparison matrix is earned only when its **Definition of Done** (§4) passes. Until then, documentation must label the feature *planned* / *implemented* / *fixture-tested* / *live-tested*.

---

## 2. Architecture decisions

These should be recorded as ADRs in `docs/adr/` during M0.

| # | Decision | Choice | Rationale |
|---|---|---|---|
| ADR-1 | Runtime language | **Python 3.12+ (asyncio)**, FastAPI (HTTP) + `grpcio` aio (gRPC) | Best LLM/MCP ecosystem (official MCP SDK). The workload is I/O-bound. Hot paths can move to Go later behind the same proto. |
| ADR-2 | Canonical contract | **Protobuf (`uar.v1`) is the source of truth.** OpenAPI and JSON Schemas are generated from it. | Honors "one protocol". gRPC stubs for Java/.NET/Go/Rust come for free. JSON mapping for Python/Node/browser. |
| ADR-3 | Client transports | **gRPC** (server-streaming) + **HTTP/JSON with SSE** + **WebSocket** (bidirectional: approvals and cancel on a live session). All three call one service layer. | Meets the pasted spec (gRPC + JSON + WebSocket) without splitting logic. |
| ADR-4 | Plugin protocol | **gRPC `uar.plugin.v1.Plugin`**, out-of-process only | A plugin can be written in any language and never runs inside the gateway process (it's isolated). |
| ADR-5 | Tool protocol | **MCP** (pinned revision, verified in M0) over stdio (local, supervised) and Streamable HTTP (remote). Native tool plugins are the gRPC alternative. | MCP is the ecosystem standard. Native plugins cover tools that aren't MCP servers. |
| ADR-6 | Persistence | **PostgreSQL** for config, runs, checkpoints, events, audit and leased job queue. No broker until benchmarks justify one. | One stateful dependency and transactional checkpoints. |
| ADR-7 | Expression language | **CEL** (`${ ... }`) for graph data mapping and conditions. Mustache-style templates for prompts only. | Sandboxed, non-Turing-complete, with implementations in every SDK language. No `eval`. |
| ADR-8 | Policy engine | Built-in YAML policies evaluated in-process (v1), behind a `PolicyDecisionPoint` interface. OPA/Cedar adapter is optional later. | No extra infrastructure for the MVP; the engine can be swapped later. |
| ADR-9 | Observability | **OpenTelemetry** traces (GenAI semantic conventions) + Prometheus metrics + structured JSON logs | Vendor-neutral. Works with Grafana, Jaeger and Langfuse-style tools. |
| ADR-10 | Provider adapters | One **OpenAI-compatible adapter** serves LM Studio, llama.cpp `llama-server`, vLLM, Groq and most `enterprise:` endpoints. Native adapters for Ollama, OpenAI Responses, Anthropic, Azure OpenAI and Vertex AI. | Cuts adapter count roughly in half. Per-provider quirks become config and conformance tests. |
| ADR-11 | Sandboxing | Tools and plugins run in **Linux containers**: read-only rootfs, no network by default, seccomp, CPU/mem limits, optional gVisor `runsc`. On Windows, local mode runs them in Docker Desktop, not as bare processes. | Real isolation on the developer's Windows laptop as well as in the cluster. |
| ADR-12 | SDK strategy | Generated stubs from proto + a thin hand-written ergonomic layer + **one shared conformance suite** run against `uar-mock` | Prevents drift across six languages. |

---

## 3. System architecture

```text
             Python  TS  Java  .NET  Go  Rust   (SDKs: generated stubs + ergonomic layer)
                 │ gRPC │ HTTP/JSON+SSE │ WebSocket
┌────────────────▼──────────────────────────────────────────────────────────┐
│ API GATEWAY   authn (API key / JWT / OIDC) · tenant binding · quotas ·    │
│               validation · idempotency · request/trace IDs                │
├──────────────┬───────────────┬────────────────┬──────────────┬────────────┤
│ AGENT ENGINE │ MODEL ROUTER  │ MCP            │ PLUGIN       │ DRY-RUN    │
│ graph compile│ class/alias   │ ORCHESTRATOR   │ RUNTIME      │ SIMULATOR  │
│ scheduler    │ policy rules  │ server registry│ manifest reg.│ static plan│
│ checkpoints  │ fallback/CB   │ session pool   │ gRPC client  │ fixtures   │
│ sub-agents   │ usage + cost  │ catalog+authz  │ health/roll- │ NO exec    │
│ approvals    │ adapters ─────┼─► model plugins│ back         │ backend    │
├──────────────┴───────────────┴────────────────┴──────────────┴────────────┤
│ GOVERNANCE  RBAC · policy decision point · approvals · redaction · audit  │
│ OBSERVABILITY  OTel traces · metrics · token/cost ledger · step logs      │
│ PERSISTENCE  PostgreSQL (runs, checkpoints, events, jobs, audit, config)  │
└──────┬─────────────────────┬────────────────────────┬─────────────────────┘
       │                     │                        │ gRPC (uar.plugin.v1)
  local: Ollama,        cloud: OpenAI,          ns uar-plugins (sandboxed):
  LM Studio, llama.cpp  Anthropic, Groq,        MCP servers · tool plugins ·
  enterprise: vLLM/…    Azure, Vertex           model plugins · agent plugins
```

**Profiles**
- `local-only`: one `uar` process (gateway + engine + router + orchestrator) + PostgreSQL. Tools and plugins run in containers. No cloud egress (enforced, not just defaulted). Runs with Docker Compose or Docker Desktop Kubernetes.
- `distributed`: gateway, engine workers, router and plugin workers as separate Deployments, with HPA on queue depth, TLS/mTLS and NetworkPolicy.

---

## 4. Feature traceability: every UAR ✔ in the comparison

This table is the contract of the plan. **DoD** is what must be demonstrably true before the ✔ may be claimed.

### 4.1 LLM inference

| Feature | Milestone | Definition of Done |
|---|---|---|
| Multi-model routing | M2 | One request shape reaches ≥ 3 providers. Resolved provider and model are returned in the response and the trace. |
| Cloud models | M2 / M7 | OpenAI, Anthropic, Groq, Azure OpenAI and Vertex adapters pass the conformance suite (sync, stream, tools, usage, errors, cancel). Live results recorded where credentials exist. |
| Local models (Ollama, LM Studio, llama.cpp) | M2 | All three live-tested on the dev machine, streaming included. Works with network egress blocked. |
| Custom model plugins | M7 | A third-party model provider written in **Go** (reference) is registered, activated, routed to and rolled back, with no gateway code change. |
| Model router rules (more flexible) | M2 → M9 | `local:` / `cloud:` / `enterprise:` classes, aliases, policy rules (tenant, data class, region, budget), capability matching, opt-in fallback, circuit breakers, cost- or latency-aware selection. Dry-run shows the routing decision and why. |

### 4.2 MCP tools

| Feature | Milestone | Definition of Done |
|---|---|---|
| MCP gateway | M3 | Registered MCP servers are exposed through `ListTools` / `ExecuteTool`, with per-call authorization and audit. |
| MCP tool orchestration (full) | M3 → M4 | Multi-server namespaced catalog (`fs.read`, `db.query`), session pooling/recovery, schema validation, side-effect classes (`read` / `write` / `external`) driving retries, parallel tool calls from LLM function calling, output size caps, untrusted-content marking. |
| Native tool plugins | M7 | A gRPC tool plugin (non-MCP) appears in the same catalog and obeys the same authz, sandbox and audit. |
| MCP server hosting | M3 / M10 | UAR supervises MCP server lifecycles: stdio containers locally, `Deployment`s in `uar-plugins` in the cluster, with health checks, restart and version pinning. |
| Tool sandboxing | M3 / M9 | Filesystem roots with traversal/symlink tests, no network by default, egress allowlist, CPU/mem/time limits, read-only DB credentials. A hostile-tool test suite passes. |

### 4.3 Agents

| Feature | Milestone | Definition of Done |
|---|---|---|
| Agent gateway | M4 | `StartRun` / `GetRun` / `WatchRun` / `CancelRun` over all three transports, with resumable event streams. |
| Agent execution (full engine) | M4 | Durable checkpoints, worker-kill resume, bounded loops, deadlines, cancellation, ambiguous-write → `needs_attention`, sub-agent calls, run-scoped memory. |
| Agent graph (YAML/JSON) | M0 / M4 | JSON Schema published. Registration rejects missing refs, unreachable nodes and unbounded cycles. Versions are immutable. |
| Multi-step workflows | M4 | Sequential, conditional branch, loop, sub-agent, `parallel` (fan-out/join, added M4-late) and approval nodes (M9). |
| Dry-run simulator | M5 | Static plan + seeded fixture simulation. An instrumented test proves zero model, tool, plugin or network calls. Unresolved items are reported, never invented. |
| Agent plugins | M7 | Declarative agent packages (graph + prompts + manifest) and executable agent plugins over gRPC. Both are versioned with rollback. |
| Multi-language agent SDK | M6 / M8 | All six SDKs run, stream events from and cancel a registered agent, and pass the conformance suite. |

### 4.4 SDKs

| Language | Milestone | Definition of Done (all languages) |
|---|---|---|
| Python, TypeScript | M6 | Inference (sync and stream), tool execute, agent run/watch/cancel, dry-run, typed errors, no automatic retry of non-idempotent calls, 100 % of conformance fixtures pass, runnable example, local package build. |
| Java, .NET, Go, Rust | M8 | Same as above. Publishing to Maven, NuGet, crates.io, npm or PyPI needs separate authorization. |

### 4.5 Deployment

| Feature | Milestone | Definition of Done |
|---|---|---|
| Kubernetes-native | M6 / M10 | Probes, graceful shutdown, leased jobs survive pod kill, config via ConfigMap/Secret refs. |
| Helm charts | M6 | `helm install / upgrade / rollback / uninstall` exercised with `local` and `distributed` values. PVC retention documented. |
| Docker Desktop Kubernetes | M6 | A clean Windows machine, following `docs/runbooks/docker-desktop.md`, reaches a working report-agent demo. |
| Local-only mode | M6 | With the cloud egress block enforced, inference + tools + agents + dry-run all work. Any cloud route is denied with an explicit error. |
| Plugin runtime | M7 | Register → validate (digest, schema, capabilities) → health check → activate → pin in-flight runs → rollback. |

### 4.6 Governance

| Feature | Milestone | Definition of Done |
|---|---|---|
| RBAC | M1 → M9 | Roles `viewer`, `developer`, `operator`, `admin`, `approver`, plus custom roles with permission scopes per model class, tool and agent. A deny matrix test covers every endpoint. |
| Audit logs | M1 → M9 | Intent and completion recorded for every privileged or external action. Sensitive actions fail closed if the audit write fails. Hash-chained (tamper-evident) in M9. Export API. |
| Model usage policies | M2 | Per-tenant and per-role allow/deny by class, provider and model, plus token and cost budgets with atomic reservation. |
| Tool usage policies | M3 | Per-call decision on identity + tool + exact arguments (e.g. path prefixes, SQL read-only). Approval-required rules are added in M9. |
| Agent sandboxing | M4 / M9 | Each agent version declares permissions. A run gets a scoped capability token that it can't exceed, even through sub-agents or plugins. |

### 4.7 Areas where TrueFoundry is ahead today (from §6 of the comparison)

| Area | Plan response |
|---|---|
| Enterprise governance: SSO, IAM, audit, RBAC | **In scope, M9:** OIDC SSO, group→role mapping, API keys and service accounts, tamper-evident audit. |
| SOC 2 / HIPAA / GDPR | **Controls, not certification.** M9 delivers a controls map (encryption, retention/deletion, access review, audit export, data residency routing). Certification is an organizational process outside this plan. |
| Observability: tokens, latency, cost, tracing | **In scope, M5 + M10:** OTel traces per run/node/tool/model call, Prometheus metrics, versioned price catalog, cost ledger, shipped Grafana dashboards. |
| Model deployment platform: GPU jobs, fine-tuning, registry | **Non-goal.** UAR is the runtime layer. It routes to self-hosted serving (vLLM, TGI, Triton) through `enterprise:` and doesn't train or host GPU jobs. |
| Production maturity | Earned by M10 gates: load tests, chaos tests, runbooks exercised, one validated cloud deployment. |

---

## 5. Protocol design (M0 deliverable, sketched here)

### 5.1 Public service (`proto/uar/v1/runtime.proto`)

```proto
syntax = "proto3";
package uar.v1;

service Runtime {
  rpc Infer(InferenceRequest)          returns (InferenceResponse);
  rpc InferStream(InferenceRequest)    returns (stream Event);
  rpc ExecuteTool(ToolRequest)         returns (ToolResult);
  rpc RegisterAgent(AgentSpec)         returns (AgentVersion);
  rpc StartRun(RunRequest)             returns (Run);
  rpc GetRun(GetRunRequest)            returns (Run);
  rpc WatchRun(WatchRunRequest)        returns (stream Event);   // resumable from after_seq
  rpc CancelRun(CancelRunRequest)      returns (Run);
  rpc DryRun(DryRunRequest)            returns (DryRunReport);
  rpc ListModels(ListModelsRequest)    returns (ListModelsResponse);
  rpc ListTools(ListToolsRequest)      returns (ListToolsResponse);
  rpc DecideApproval(ApprovalDecision) returns (Approval);
}

// The typed envelope from the original spec: every streamed message is an Event.
message Event {
  string run_id = 1;  uint64 seq = 2;  google.protobuf.Timestamp ts = 3;
  string trace_id = 4;
  oneof body {
    Started started = 10;           TokenDelta token = 11;
    ToolCall tool_call = 12;        ToolResult tool_result = 13;
    NodeStarted node_started = 14;  NodeCompleted node_completed = 15;
    Usage usage = 16;               ApprovalRequired approval_required = 17;
    Error error = 18;               Completed completed = 19;   // exactly one terminal event
  }
}
```

JSON/SSE carries the same messages with `"type"` set to the `oneof` field name. For example, the spec's `inference_stream` becomes:

```json
{"type":"token","run_id":"r_1","seq":7,"token":{"text":"def"}}
{"type":"usage","run_id":"r_1","seq":58,"usage":{"input":12,"output":45,"cost":{"amount":"0.0000","currency":"USD","estimated":false}}}
```

Correction to the original example: `usage` arrives once, near the end, not on every token.

### 5.2 Inference request semantics

```json
{
  "model": "local:llama3:8b",
  "messages": [{"role": "user", "content": "Write a python function"}],
  "tools": ["file.write", "browser.search"],
  "tool_mode": "suggest",
  "agent": null,
  "stream": true
}
```

- `tools` + `tool_mode: "suggest"` (default) returns tool calls to the caller as standard function calling.
- `tool_mode: "auto"` makes the gateway run a **bounded implicit agent** (max steps and budget from policy) that executes the authorized tools.
- `agent: "coding_assistant"` makes the call SDK and gateway shorthand for `StartRun` + `WatchRun` + wait. That is what makes `client.inference(model, prompt, agent=...)` in the original spec work.
- `input: "..."` (from the original spec) is accepted as sugar for a single user message.

### 5.3 Model name grammar and routing

```
model   := [class ":"] [provider "/"] name
class   := "local" | "cloud" | "enterprise"      ; split on the FIRST colon only if prefix is a class
examples: local:llama3:8b → class=local, name=llama3:8b (provider resolved by class order)
          cloud:anthropic/claude-sonnet-5 · enterprise:corp-vllm/finance-7b · cloud:default (alias)
```

```yaml
# config/router.yaml
classes:
  local:      { providers: [ollama, lmstudio, llamacpp] }
  cloud:      { providers: [openai, anthropic, groq, azure_openai, vertex] }
  enterprise: { providers: [corp_vllm] }
aliases:
  local:default: ollama/llama3:8b
  cloud:default: anthropic/claude-sonnet-5
rules:
  - { when: { data_class: confidential },      deny: [cloud] }
  - { when: { tenant: acme, role: developer }, allow: [local, enterprise] }
  - { when: { requires: [tools, vision] },     prefer: capability_match }
fallback:
  - { from: local:default, to: cloud:default, when: provider_unavailable, requires_policy: allow_cloud_fallback }
circuit_breaker: { failure_ratio: 0.5, window: 30s, cooldown: 60s }
```

### 5.4 Plugin protocol (`proto/uar/plugin/v1/plugin.proto`)

```proto
service Plugin {
  rpc Describe(DescribeRequest)     returns (PluginDescriptor);   // kind, version, capabilities, schemas
  rpc Init(PluginInitRequest)       returns (PluginInitResponse); // validated config + resolved secret refs
  rpc Execute(PluginExecuteRequest) returns (PluginExecuteResponse);
  rpc ExecuteStream(PluginExecuteRequest) returns (stream PluginEvent);
  rpc Health(HealthRequest)         returns (HealthResponse);
  rpc Shutdown(ShutdownRequest)     returns (ShutdownResponse);
}
message PluginExecuteRequest {
  ExecutionContext ctx = 1;   // tenant, run_id, deadline, idempotency_key, capability_token, trace ctx
  oneof call { ModelCall model = 10; ToolCall tool = 11; AgentCall agent = 12; }
}
```

```yaml
# plugin.yaml (manifest)
id: acme.sentiment-model
version: 1.3.0                    # immutable
kind: model                       # model | tool | agent
runtime: { api: uar.plugin.v1, image: ghcr.io/acme/sentiment@sha256:… }
capabilities: { network: [], filesystem: none, secrets: [ACME_TOKEN] }
limits: { cpu: "500m", memory: 512Mi, timeout: 30s, concurrency: 8 }
schemas: { config: ./config.schema.json, input: ./in.schema.json, output: ./out.schema.json }
```

Plugin SDKs (thin server helpers) ship for Python, Go and TypeScript in M7, and for Java and .NET in M8.

### 5.5 Agent graph (the original example, made executable)

```yaml
apiVersion: uar/v1
kind: Agent
metadata: { id: coding_assistant, version: 1.0.0 }
spec:
  input:
    type: object
    required: [task, path]
    properties: { task: {type: string}, path: {type: string} }
  permissions: { models: ["local:*"], tools: ["file.write"], agents: [] }
  limits: { max_steps: 20, max_loop_iterations: 5, max_tokens: 20000, timeout: 120s, max_cost_usd: 0.50 }
  memory: { scope: run }                     # run | agent (opt-in, retention required)
  nodes:
    - id: llm_step
      type: llm
      model: local:llama3:8b
      prompt: "Write a Python function for: {{ input.task }}. Return JSON {code}."
      output_schema: { type: object, required: [code], properties: { code: {type: string} } }
    - id: tool_step
      type: tool
      tool: file.write
      args: { path: "${ input.path }", content: "${ nodes.llm_step.output.code }" }
    - id: done
      type: return
      value: "${ {'path': input.path} }"
  edges:
    - { from: llm_step, to: tool_step }
    - { from: tool_step, to: done }
```

Node types: `llm`, `tool`, `agent` (sub-agent with an inherited, narrowed capability token), `transform`, `condition`, `loop`, `parallel`, `approval`, `return`. Edges may carry a `when: ${ CEL }` guard.

---

## 6. Milestones

Durations are **indicative**, assuming 3–4 engineers. Confirm them in M0 after capacity and hardware are known. Each milestone ends with an exit-gate report in `docs/gates/`.

| M | Name | ~Weeks | Features delivered (§4) |
|---|---|---|---|
| M0 | Foundations & contracts | 2 | Protos, JSON Schemas (graph, plugin, router), ADRs, threat model, conformance fixtures v0 |
| M1 | Runtime core & governance base | 3 | Gateway (3 transports), authn (API key/JWT), tenants, RBAC v1, audit v1, persistence, jobs |
| M2 | Model router | 3 | Routing classes, local models, OpenAI + Anthropic, model policies, usage/cost ledger |
| M3 | MCP orchestrator & sandbox | 3 | MCP gateway, orchestration, server hosting (local), tool sandboxing, tool policies |
| M4 | Agent engine | 4 | Agent gateway, full engine, graphs, multi-step workflows, agent sandboxing v1 |
| M5 | Dry-run & observability v1 | 2 | Dry-run simulator, OTel traces, metrics, step logs |
| M6 | **MVP**: packaging + first SDKs | 3 | Python/TS SDKs, Helm, Docker Desktop K8s, local-only mode |
| M7 | Plugin system & remaining providers | 4 | Plugin runtime, custom model/tool/agent plugins, Groq/Azure/Vertex/LM Studio/llama.cpp conformance |
| M8 | SDK wave 2 | 4 (parallel) | Java, .NET, Go, Rust SDKs + Java/.NET plugin helpers |
| M9 | Enterprise governance | 3 | OIDC SSO, RBAC v2, approvals, tamper-evident audit, router rules v2, compliance controls map |
| M10 | Distributed & production readiness | 4 | Distributed profile, cloud MCP hosting, dashboards, load/chaos tests, one cloud validated |

MVP ≈ 20 weeks. Full scope ≈ 32 weeks with M8 running in parallel with M9 (add contingency when committing dates).

### M0: Foundations & contracts
- **Work:** the ADRs in §2; `runtime.proto`, `plugin.proto`; generate OpenAPI and JSON Schemas in CI (`buf` lint and breaking-change check); graph, plugin and router schemas with valid/invalid examples; role × permission matrix; threat model (STRIDE for gateway, tools, plugins, simulation); data retention policy; confirm the current MCP spec revision and SDK version; laptop resource inventory; performance targets.
- **Exit gate:** each of these is expressible, and validates, against the contracts: an inference call, a streamed tool-calling inference, the report-agent run, a policy denial, a cancellation, an approval and a dry-run. The breaking-change check runs in CI.

### M1: Runtime core & governance base
- **Work:** monorepo, CI (lint, type check, tests, container build), Postgres migrations, config validation, health/readiness; gateway serving gRPC + HTTP/SSE + WebSocket from one service layer; API keys (hashed) and JWT; tenant binding from identity only; RBAC v1; audit writer (fails closed); structured errors (`code`, `message`, `request_id`, `retryable`); idempotency keys; request size limits and quotas; OTel trace-ID propagation from day one; leased job queue with fencing tokens.
- **Exit gate:** clean checkout → `make up` → healthy. Cross-tenant reads and cancels are denied. Unauthorized calls return 401/403 on all three transports. An audit failure blocks the sensitive action. The same fixture passes over gRPC and HTTP.

### M2: Model router
- **Work:** adapter interface (chat, stream, tools/function calling, usage, cancel, capabilities); adapters for Ollama, OpenAI-compatible (LM Studio, llama.cpp), OpenAI Responses, Anthropic; the §5.3 grammar, aliases, rules, capability matching, opt-in fallback, circuit breaker; versioned price catalog; atomic budget reservation; usage normalization with estimated vs reported flags; `tool_mode: suggest`.
- **Exit gate:** live local inference (sync and stream) through all three local backends on the dev machine. Cloud adapters pass the recorded-fixture conformance suite (live runs are opt-in). A confidential-data request to `cloud:` is denied. Fallback happens only when policy allows it. Stopping a stream never triggers a second generation.

### M3: MCP orchestrator & sandbox
- **Work:** admin-only server registry (no client-supplied commands or URLs); stdio (containerized) + Streamable HTTP transports; session pool with recovery; namespaced catalog and discovery cache; JSON Schema argument validation; side-effect classes; per-call authorization on exact arguments; intent/completion audit; output caps and untrusted-content tagging; first-party `fs` (rooted) and `db` (read-only, parameterized, row/time limits) servers; egress allowlist with DNS-rebinding and redirect guards; `tool_mode: auto` (bounded).
- **Exit gate:** reading a fixture file and querying a fixture DB work through the API. Traversal, symlink escape, forbidden root, unauthorized write, unregistered endpoint and egress to a non-allowlisted host are all rejected, with audit entries. A killed MCP server produces a bounded, actionable error, and the session recovers.

### M4: Agent engine
- **Work:** graph compiler (reference checks, reachability, bounded cycles, CEL type-check); node executors for all §5.5 types except `approval`; per-node checkpoints committed atomically with events; resume from checkpoint; cancellation; retry policy by side-effect class; ambiguous write → `needs_attention` with a recovery API; sub-agents with narrowed capability tokens and depth limits; `parallel` fan-out/join with bounded concurrency; run-scoped memory; `agent:` shorthand on inference.
- **Exit gate:** the report agent runs DB query → local LLM → authorized file write. Killing a worker between nodes resumes the run with no repeated node. Killing it after the write but before the acknowledgment yields `needs_attention` and no second write. Runaway loops stop at their limits. A sub-agent can't use a tool its parent lacks. Duplicate requests don't create duplicate runs.

### M5: Dry-run & observability v1
- **Work:** separate `SimulationBackend` with no import path to execution adapters (enforced by an import-linter rule); static plan (branches, routing decisions, permissions needed, token and cost ranges, uncertainty); seeded fixture simulation; unresolved items marked, never invented. OTel spans for run → node → model/tool call, with GenAI attributes; Prometheus metrics (latency p50/p95/p99, tokens, cost, errors, queue delay); step logs through `WatchRun`.
- **Exit gate:** with every execution entry point instrumented to fail on call, dry-run of all example agents succeeds, and missing fixtures show as `unresolved`. One agent run produces a single connected trace in Jaeger. Token and cost totals in metrics match the ledger.

### M6: MVP (packaging + Python/TypeScript SDKs)
- **Work:** `uar-mock` server that replays conformance fixtures; Python and TS SDKs (generated + ergonomic API exactly as in the original examples, `client.inference(model=..., prompt=..., agent=...)`); Dockerfiles (pinned); Helm chart with `local` values (Postgres PVC, probes, limits, migrations job, optional ingress); `local-only` profile with an enforced egress block; Windows/Docker Desktop runbook; examples `in-app-assistant` and `report-generator`.
- **Exit gate:** on a fresh Docker Desktop Kubernetes cluster, with no cloud credentials: inference, streaming, tools, the report agent and dry-run all work from both SDKs. Helm install, upgrade, rollback and uninstall are exercised, with PVC retention documented. MVP release artifacts are built locally (publishing needs authorization).

### M7: Plugin system & remaining providers
- **Work:** plugin registry (manifest validation, image digest verification, capability review); plugin supervisor (container per plugin in `uar-plugins`, health, restart); activation and rollback with run pinning; model, tool and agent plugin routing through the existing router, catalog and engine; declarative agent packages; plugin SDK helpers (Python, Go, TS); reference plugins (Go model plugin, TS tool plugin, Python agent plugin); Groq, Azure OpenAI and Vertex adapters, plus a conformance report for LM Studio and llama.cpp.
- **Exit gate:** the Go model plugin serves `enterprise:acme/sentiment` without gateway changes. Upgrading mid-run keeps the in-flight run on the old version, and rollback serves new runs from the prior version. A plugin that requests an undeclared capability is refused at activation. A plugin that hangs is timed out and circuit-broken.

### M8: SDK wave 2 (runs in parallel with M9)
- **Work:** Java (gRPC-java + `UARClient`), .NET (Grpc.Net.Client + `UarClient`, `IAsyncEnumerable` streams), Go (context-first), Rust (tonic, async streams). Each gets the ergonomic API, typed errors, cancellation, examples and conformance CI. Java and .NET get plugin SDK helpers.
- **Exit gate:** all six SDKs pass 100 % of the conformance suite against `uar-mock` and a live `uar`. The original Java/Node/Python snippets compile or run essentially unchanged.

### M9: Enterprise governance
- **Work:** OIDC SSO with group→role mapping; service accounts; RBAC v2 with custom roles; `approval` node and inline approvals, bound to action + argument hashes, with expiry and no replay; hash-chained append-only audit with export; router rules v2 (region, data residency, cost/latency optimization); content redaction policies; retention and deletion jobs; compliance controls map; examples `code-assistant` and `enterprise-workflow` (both approved and denied paths shown in audit).
- **Exit gate:** SSO login maps to the correct roles. An approval can't be replayed with changed arguments. Tampering with any audit row is detected by verification. Retention deletes run content on schedule while audit metadata is kept as configured. The cross-tenant suite covers API, jobs, events, artifacts, credentials and plugins.

### M10: Distributed & production readiness
- **Work:** separate Deployments (gateway, engine workers, router, plugin supervisor); mTLS between services; enforced NetworkPolicy (with a check that the CNI actually enforces it); secret-manager integration; MCP server hosting as Deployments; HPA on queue depth; backup and restore; Grafana dashboards (tokens, cost, latency, tool and agent failures); load tests (k6/Locust) that separate runtime overhead from model time; chaos tests (worker kill, DB failover, provider outage); runbooks; one authorized cloud deployment (cost estimate approved first).
- **Exit gate:** acknowledged jobs survive worker termination and dependency faults. Backup and restore recover state and event history. Load results meet the M0 targets, with deviations published. Every runbook has been exercised once. The chosen cloud is validated, and other clouds stay "documented target".

---

## 7. Repository layout

```text
proto/uar/v1/ · proto/uar/plugin/v1/     canonical contracts (buf)
contracts/                               generated OpenAPI + JSON Schemas, conformance fixtures
runtime/uar/{gateway,engine,router,mcp,plugins,governance,simulation,observability,store}/
runtime/tests/{unit,contract,integration,recovery,security,simulation}/
migrations/
mock/                                    uar-mock fixture server
sdks/{python,typescript,java,dotnet,go,rust}/
plugin-sdks/{python,go,typescript,java,dotnet}/
plugins/reference/{go-model,ts-tool,py-agent}/
mcp-servers/{fs,db}/
deploy/{docker,helm/uar,values/{local,distributed}}/
examples/{in-app-assistant,report-generator,code-assistant,enterprise-workflow}/
tests/{e2e,load,chaos}/
docs/{adr,gates,runbooks,capability-matrix.md}
```

---

## 8. Quality strategy

| Layer | What | Where |
|---|---|---|
| Contract | `buf breaking`; generated OpenAPI diff; conformance fixtures shared by all SDKs and transports | CI, every PR |
| Provider conformance | Recorded fixtures by default; live suites opt-in per provider; results in `docs/capability-matrix.md` | CI + manual |
| Security | Tenant isolation, RBAC deny matrix, traversal/symlink, SSRF/egress, prompt-injection in tool output, plugin capability escape, approval replay | CI |
| Recovery | Worker kill at each checkpoint boundary, ambiguous write, lease loss, stream interruption | CI (deterministic) + chaos (M10) |
| Simulation safety | Execution entry points trip on call during dry-run; import-linter boundary | CI |
| Performance | p50/p95/p99 gateway overhead, queue delay, throughput at N concurrent runs; hardware and model disclosed | M5 baseline, M10 gate |

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| Three transports triple the surface | One service layer; transports are thin adapters; the same fixtures run over each |
| Six SDKs drift | Proto-generated core, shared conformance suite, `uar-mock`, contract CI blocks merges |
| "OpenAI-compatible" providers differ in edge cases | Per-provider conformance config; unsupported features rejected explicitly |
| Plugins or tools escape the sandbox | Containers + seccomp + no-net default + capability tokens; gVisor option; hostile-code submissions excluded from the trust model |
| Windows laptop limits (RAM, no GPU) | Small default model (≤ 8B quantized); host-run Ollama/LM Studio option; measure in M0 |
| Simulation seen as prediction | Explicit `unresolved` and ranges; UI and docs wording; no fabricated outputs |
| Comparison claims outrun reality | `Mission_Comparison.md` cells link to the capability matrix; ✔ only after its DoD gate passes |
| Python performance ceiling | Measure in M5/M10; the proto boundary allows porting gateway or router to Go without SDK changes |

---

## 10. Non-goals for this plan

Model training and fine-tuning, GPU job scheduling, a model registry, billing and payments, a public plugin marketplace, a graphical workflow editor, active-active multi-region operation, arbitrary code in graph expressions, and compliance certification itself.

## 11. Decisions needed from the product owner before M0 closes

1. Team size and target dates, which convert the indicative weeks into commitments.
2. The target cloud for M10 (AKS, EKS or GKE) and budget approval.
3. Which enterprise model endpoint(s) `enterprise:` must support first.
4. Whether WebSocket is needed in the MVP (the plan puts it in M1) or can wait until M9 approvals.
5. Whether the lost Python prototype can be recovered, which would shorten M1–M6.
