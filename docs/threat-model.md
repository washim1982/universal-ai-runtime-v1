# Threat model (MVP, STRIDE)

**Assets:** tenant data in runs and events, tool side effects (files written), provider credentials,
audit trail, model spend. **Actors:** authenticated callers of varying roles, other tenants,
operators, MCP servers/tools (untrusted output), model output (untrusted), a network attacker on
the host. **Out of scope for the MVP:** actively hostile third-party plugin code, multi-host
deployments, hostile local processes racing the filesystem.

| Threat | Component | Mitigation (MVP) | Verified by | Residual / later |
|---|---|---|---|---|
| **S**poofing a tenant | gateway | Tenant derived only from the credential (API key hash / JWT claim), never from request data | `test_jwt_bearer_and_tenant_binding`, cross-tenant tests, `test_oidc_*`, `test_sts.py` (tenant taken from the application registration; forged, tampered and algorithm-confused tokens refused) | Individual STS tokens cannot be revoked before expiry except by disabling the application (keep lifetimes short) |
| Key theft from DB/config | governance | Only SHA-256 of keys stored; keys in `.local/credentials.env` (git-ignored) | — | Key rotation runbook (M10) |
| **T**ampering with run state by a stale worker | engine | Lease `lease_version` fencing on every checkpoint and event write | `test_stale_worker_cannot_commit` + mutation check | — |
| Tampering with audit | store | No update/delete path in code | — | Hash chain + restricted DB role (M9) |
| **R**epudiation of actions | governance | Intent row before every sensitive action (fail closed), completion after; request ids | `test_audit_failure_blocks_sensitive_action` | Export API (M9) |
| **I**nformation disclosure across tenants | store | Every query takes the tenant; 404 for foreign ids | `test_cross_tenant_run_access_denied` | Row-level security (M9) |
| Secrets in logs/errors | errors/logging | Redaction of sensitive keys; audit stores arg hashes and keys, not values | `test_audit_rows_written_without_payloads` | Configurable content redaction (M9) |
| Path traversal / link escape / Windows ADS | fs MCP server | Relative paths only, no `..`, `:`, NUL, drive letters; per-component link/junction check; resolved path must stay in root | `test_traversal_rejected`, `test_link_escape_rejected` | TOCTOU vs hostile local processes |
| SQL writes / exfiltration | db MCP server | `mode=ro` + SQLite authorizer allowing only SELECT/READ/FUNCTION; row and time caps | `test_database_read_only` | — |
| SSRF via MCP URLs | orchestrator | Servers from operator config only; http(s) only; every resolved address must be public or allow-listed; no redirects | `test_ssrf_guard`, `test_streamable_http_transport` | DNS TOCTOU between check and connect |
| Prompt injection via tool output | engine/service | Tool output marked `untrusted`, passed only as tool messages; tools gated by policy regardless of model intent; agent permissions fixed at registration | policy tests | Output classifiers (later) |
| **D**enial of service | gateway/router | Body size cap, per-principal rate and concurrency limits, provider semaphores with queue timeout, circuit breakers, step/loop/token/time/tool-call limits | `test_request_limits`, `test_rate_limit_returns_429`, loop/limit tests | Limits are per process (distributed quotas M10) |
| Runaway spend | router | Atomic daily token reservation; per-run `max_tokens`, `max_cost_usd` (refused when a price is unknown) | `test_daily_token_budget_is_atomic` | Provider-side usage can differ slightly |
| **E**levation via sub-agents | engine | Sub-agent tools must match every ancestor's permissions | `test_subagent_cannot_exceed_parent_permissions` | — |
| Elevation via a revoked key mid-run | engine | Credential re-checked when a run is (re)claimed | `test_revoked_credential_stops_run` | Re-check before each node (M9) |
| Tool process escape | sandbox | Container: no network, read-only rootfs, caps dropped, non-root, memory/pids limits | `test_container_sandbox` | gVisor (M9); dev config uses process mode |
| Dry-run causing side effects | simulation | Separate package with no import path to execution; tripwire tests | dry-run tests + mutation check | — |
