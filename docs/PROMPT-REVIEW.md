# UAR implementation prompt review

The original prompt is a useful product vision, but it is not yet an executable engineering specification. Preserve its unified API, provider adapters, MCP integration, graph execution, plugins, governance, simulation, six SDKs, and Kubernetes deployment. Add contracts, scope boundaries, failure semantics, and release gates before implementation.

## Changes incorporated

| Original gap | Enhancement | Why it matters |
| --- | --- | --- |
| All capabilities appear equally urgent | Local MVP, extensible beta, cloud release | Establishes a testable first delivery without dropping the full vision. |
| One pod per component prescribed upfront | Logical modules first; separate deployment profile later | Reduces prototype overhead while preserving service boundaries. |
| “Maintain state” and “support loops” | Durable checkpoints, bounded loops, cancellation, recovery, and uncertain tool outcomes | Prevents lost runs and accidental repeated side effects. |
| Four endpoints only | Versioned contracts plus run status, events, cancellation, registration, and approvals | Makes long-running agents usable by applications. |
| Governance added near the end | Identity, authorization, tenant scoping, and audit from the first slice | Avoids retrofitting trust boundaries. |
| Generic tool execution | MCP negotiation, transport lifecycle, schema validation, and per-call authorization | Distinguishes protocol support from an arbitrary HTTP proxy. |
| Unspecified plugin execution | Immutable manifests, validation, isolation, explicit capabilities, rollback | Installing a plugin must not grant arbitrary host access. |
| “Dry-run” is undefined | Offline static planning and fixture simulation; zero external execution | Makes previews predictable and safe to rely on. |
| Hardcoded model names | Configurable aliases and capability-based routing | Provider catalogs and supported features change. |
| Six SDKs developed together | Shared OpenAPI contract; two SDKs first, four before full release | Reduces contract drift and makes adoption testable. |
| Free cloud deployment assumed | Cost-aware local profile and explicit cloud estimates | Infrastructure, storage, egress, GPUs, and model calls can incur costs. |
| Tests named without pass criteria | Phase exit gates and documented performance measurements | Makes completion objectively reviewable. |

## Corrections to examples and deployment assumptions

- Inference is a POST endpoint; the original bare `curl` command would make a GET request and does not validate inference.
- Tool inputs should consistently live under `args`; SDK and API examples must use the same schema.
- Kubernetes Services select workloads; do not create a separate Service for every pod replica. Use Deployments for stateless processes and appropriate persistent storage for stateful components.
- Make ingress optional and document its controller prerequisite. Use port forwarding for the first local smoke test.
- LM Studio can be an external host endpoint; its official documentation also describes the headless `llmster` daemon. Do not assume a desktop GUI application must run in a pod. [LM Studio documentation](https://lmstudio.ai/docs/developer)
- Pin the MCP revision and SDK compatibility. Specify stdio for supervised local processes and Streamable HTTP for remote servers. [MCP transport specification](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-11-25/basic/transports.mdx)
- Qualify the free-prototyping goal: Docker Desktop use depends on its license terms. Avoid promising free cloud clusters or free inference. [Docker Desktop licensing](https://docs.docker.com/subscription-billing/desktop-license/)

## Proposed decisions

The stack and sequencing in the companion documents are recommendations, not existing project constraints. Start with Python/FastAPI, PostgreSQL-backed jobs and state, HTTP/SSE contracts, Helm, and Node/Python SDKs. Keep third-party tool execution in separate processes or containers. Review these decisions in phase 0 and record any changes before coding.

The repository was empty when reviewed. No implementation, provider integration, deployment, or benchmark has been verified. This deliverable is the revised specification and implementation plan.
