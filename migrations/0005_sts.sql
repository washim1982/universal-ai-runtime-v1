-- Built-in security token service (STS): application registrations, their client secrets, and the
-- keys that sign access tokens.

CREATE TABLE app_registrations (
    client_id     text        PRIMARY KEY,
    tenant        text        NOT NULL,
    name          text        NOT NULL,
    description   text        NOT NULL DEFAULT '',
    roles         text[]      NOT NULL,
    token_ttl_s   integer     NOT NULL,
    created_by    text        NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_token_at timestamptz,
    disabled_at   timestamptz,
    disabled_by   text
);
CREATE INDEX app_registrations_tenant ON app_registrations (tenant, created_at DESC);
CREATE UNIQUE INDEX app_registrations_name ON app_registrations (tenant, lower(name)) WHERE disabled_at IS NULL;

-- Only the SHA-256 of a secret is stored (secrets are 256-bit random values).
CREATE TABLE app_secrets (
    secret_id  text        PRIMARY KEY,
    client_id  text        NOT NULL REFERENCES app_registrations(client_id) ON DELETE CASCADE,
    sha256     text        NOT NULL,
    hint       text        NOT NULL,
    created_by text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    revoked_at timestamptz
);
CREATE INDEX app_secrets_client ON app_secrets (client_id);

-- Token signing keys (ES256). private_key holds the PEM, encrypted when sts.key_encryption_env is set.
CREATE TABLE sts_signing_keys (
    kid          text        PRIMARY KEY,
    alg          text        NOT NULL,
    private_key  text        NOT NULL,
    encrypted    boolean     NOT NULL,
    public_jwk   jsonb       NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    active_from  timestamptz NOT NULL,       -- used for signing from this time
    retire_after timestamptz                 -- tokens it signed are no longer accepted after this time
);
