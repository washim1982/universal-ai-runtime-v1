-- Plugins: immutable versions, one active version per plugin, and per-run version pins.

CREATE TABLE plugin_versions (
    plugin_id   text        NOT NULL,
    version     text        NOT NULL,
    type        text        NOT NULL CHECK (type IN ('model','tool','agent')),
    digest      text        NOT NULL,
    manifest    jsonb       NOT NULL,
    status      text        NOT NULL DEFAULT 'registered'
                  CHECK (status IN ('registered','active','inactive','failed')),
    message     text,
    created_by  text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plugin_id, version)
);

CREATE TABLE plugin_active (
    plugin_id        text        PRIMARY KEY,
    version          text        NOT NULL,
    previous_version text,
    activated_by     text        NOT NULL,
    activated_at     timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (plugin_id, version) REFERENCES plugin_versions (plugin_id, version)
);

-- Plugin versions a run started with ({plugin_id: version}); upgrades affect new runs only.
ALTER TABLE runs ADD COLUMN plugins jsonb NOT NULL DEFAULT '{}'::jsonb;
