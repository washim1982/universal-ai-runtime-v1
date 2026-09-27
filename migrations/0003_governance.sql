-- M9: approvals, tamper-evident audit chain, retention anchors.

-- An approval is bound to one action on one run node and to the hash of its arguments. It is decided
-- once (pending -> approved | rejected | expired | cancelled) and, when approved, consumed once.
CREATE TABLE approvals (
    approval_id    text        PRIMARY KEY,
    tenant         text        NOT NULL,
    run_id         text        NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    node_id        text        NOT NULL,
    kind           text        NOT NULL CHECK (kind IN ('node', 'tool')),
    action         text        NOT NULL,
    args_hash      text        NOT NULL,
    summary        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    approver_roles text[]      NOT NULL DEFAULT '{}',
    requested_by   text        NOT NULL,
    status         text        NOT NULL CHECK (status IN ('pending','approved','rejected','expired','cancelled')),
    decided_by     text,
    decided_at     timestamptz,
    comment        text,
    consumed_at    timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz NOT NULL,
    CHECK (consumed_at IS NULL OR status = 'approved')
);
CREATE INDEX approvals_tenant ON approvals (tenant, status, created_at DESC);
CREATE INDEX approvals_run ON approvals (run_id, node_id, created_at DESC);
CREATE INDEX approvals_expiry ON approvals (expires_at) WHERE status = 'pending';
-- At most one open approval per run node.
CREATE UNIQUE INDEX approvals_one_pending ON approvals (run_id, node_id) WHERE status = 'pending';

-- Audit hash chain: per tenant, seq increases by one and
--   hash = sha256(prev_hash || canonical_json(row)).
-- Rows written before this migration keep seq NULL ("unchained") and are reported by verification.
ALTER TABLE audit ADD COLUMN seq bigint, ADD COLUMN prev_hash text, ADD COLUMN hash text;
CREATE UNIQUE INDEX audit_chain ON audit (tenant, seq) WHERE seq IS NOT NULL;

CREATE TABLE audit_heads (
    tenant text   PRIMARY KEY,
    seq    bigint NOT NULL,
    hash   text   NOT NULL
);

-- Retention may prune the oldest chained rows; the last pruned (seq, hash) is kept so the remaining
-- chain still verifies from a known point.
CREATE TABLE audit_anchors (
    tenant     text        PRIMARY KEY,
    seq        bigint      NOT NULL,
    hash       text        NOT NULL,
    pruned     bigint      NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Append-only at the database level. Retention deletes inside a transaction that sets
-- uar.audit_retention = 'on'. (The hash chain detects changes made by anyone who bypasses this.)
CREATE FUNCTION audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('uar.audit_retention', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'audit rows are append-only';
END
$$;
CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit
    FOR EACH ROW EXECUTE FUNCTION audit_append_only();
