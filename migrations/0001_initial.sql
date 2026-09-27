-- UAR schema v1. Every row is tenant-scoped; queries must filter by tenant.

CREATE TABLE agents (
    tenant       text        NOT NULL,
    agent_id     text        NOT NULL,
    version      text        NOT NULL,
    digest       text        NOT NULL,
    definition   jsonb       NOT NULL,
    created_by   text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    seq          bigserial,
    PRIMARY KEY (tenant, agent_id, version)
);

CREATE TABLE runs (
    run_id           text        PRIMARY KEY,
    tenant           text        NOT NULL,
    agent_id         text        NOT NULL,
    version          text        NOT NULL,
    status           text        NOT NULL CHECK (status IN
                       ('queued','running','waiting_approval','succeeded','failed','cancelled','needs_attention')),
    input            jsonb       NOT NULL,
    output           jsonb,
    error            jsonb,
    usage            jsonb       NOT NULL DEFAULT '{}'::jsonb,
    steps            integer     NOT NULL DEFAULT 0,
    current_node     text,
    checkpoint       jsonb,
    principal        jsonb       NOT NULL,          -- identity snapshot: subject, roles, key id
    parent_run_id    text        REFERENCES runs(run_id),
    depth            integer     NOT NULL DEFAULT 0,
    idempotency_key  text,
    request_hash     text,
    cancel_requested boolean     NOT NULL DEFAULT false,
    lease_owner      text,
    lease_expires_at timestamptz,
    lease_version    bigint      NOT NULL DEFAULT 0, -- fencing token
    attempts         integer     NOT NULL DEFAULT 0,
    deadline_at      timestamptz,
    traceparent      text,
    request_id       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz
);
CREATE UNIQUE INDEX runs_idem ON runs (tenant, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX runs_claimable ON runs (created_at) WHERE status IN ('queued','running') AND parent_run_id IS NULL;
CREATE INDEX runs_tenant ON runs (tenant, created_at DESC);

CREATE TABLE run_events (
    run_id  text        NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    seq     integer     NOT NULL,
    ts      timestamptz NOT NULL DEFAULT now(),
    body    jsonb       NOT NULL,
    PRIMARY KEY (run_id, seq)
);

-- Intent is recorded before any write/external tool call; completion afterwards.
-- An intent without completion after a crash is an ambiguous outcome.
CREATE TABLE tool_intents (
    intent_id    text        PRIMARY KEY,
    tenant       text        NOT NULL,
    run_id       text        REFERENCES runs(run_id) ON DELETE CASCADE,
    node_id      text,
    tool         text        NOT NULL,
    side_effect  text        NOT NULL,
    args_hash    text        NOT NULL,
    status       text        NOT NULL CHECK (status IN ('intent','completed','failed','resolved')),
    result       jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);
CREATE INDEX tool_intents_run ON tool_intents (run_id, node_id);

CREATE TABLE audit (
    id             bigserial   PRIMARY KEY,
    ts             timestamptz NOT NULL DEFAULT now(),
    tenant         text        NOT NULL,
    actor          text        NOT NULL,
    action         text        NOT NULL,
    target         text        NOT NULL,
    outcome        text        NOT NULL,
    request_id     text,
    run_id         text,
    policy_version text,
    details        jsonb       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX audit_tenant_ts ON audit (tenant, ts DESC);
-- Append-only for the application role: no UPDATE/DELETE path exists in code.
-- Tamper evidence (hash chain) is an M9 deliverable.

CREATE TABLE usage_ledger (
    id            bigserial   PRIMARY KEY,
    ts            timestamptz NOT NULL DEFAULT now(),
    tenant        text        NOT NULL,
    subject       text        NOT NULL,
    request_id    text,
    run_id        text,
    provider      text        NOT NULL,
    model         text        NOT NULL,
    input_tokens  integer     NOT NULL,
    output_tokens integer     NOT NULL,
    cost          numeric(18,8),               -- NULL = unknown price, never zero by default
    currency      text,
    estimated     boolean     NOT NULL,
    price_version text
);
CREATE INDEX usage_tenant_ts ON usage_ledger (tenant, ts DESC);

-- Atomic daily token budget per tenant: reserve before a call, settle after.
CREATE TABLE tenant_budget (
    tenant   text    NOT NULL,
    day      date    NOT NULL,
    reserved bigint  NOT NULL DEFAULT 0,
    used     bigint  NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant, day)
);

-- Request-level idempotency for synchronous operations (tool execute).
CREATE TABLE idempotency (
    tenant       text        NOT NULL,
    operation    text        NOT NULL,
    key          text        NOT NULL,
    request_hash text        NOT NULL,
    response     jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant, operation, key)
);
