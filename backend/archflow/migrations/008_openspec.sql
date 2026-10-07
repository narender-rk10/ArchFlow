-- 008_openspec: OpenSpec files (living specs, changes, archive) and change metadata (see archflow/openspec_service.py).
CREATE TABLE IF NOT EXISTS openspec_files (
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,                    -- e.g. openspec/specs/order-lookup/spec.md
    content     TEXT NOT NULL,
    updated_by  TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, path)
);

CREATE TABLE IF NOT EXISTS openspec_changes (
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    change_id   TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'valid', 'invalid', 'archived')),
    engine      TEXT NOT NULL DEFAULT '',
    branch      TEXT NOT NULL DEFAULT 'main',
    scope       JSONB NOT NULL DEFAULT '{}'::jsonb,
    validation  JSONB NOT NULL DEFAULT '{}'::jsonb,
    archived_as TEXT,
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, change_id)
);
CREATE INDEX IF NOT EXISTS idx_openspec_changes_project ON openspec_changes (project_id, updated_at DESC);
