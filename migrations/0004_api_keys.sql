-- API keys managed through the admin API (config-file keys keep working alongside them).
-- Only the SHA-256 of a key is stored; the secret is shown once, when the key is created.
CREATE TABLE api_keys (
    key_id      text        PRIMARY KEY,
    tenant      text        NOT NULL,
    sha256      text        NOT NULL,
    subject     text        NOT NULL,
    roles       text[]      NOT NULL,
    description text        NOT NULL DEFAULT '',
    created_by  text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz,
    revoked_at  timestamptz,
    revoked_by  text
);
CREATE INDEX api_keys_tenant ON api_keys (tenant, created_at DESC);
