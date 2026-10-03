-- 004_decisions: architecture decision records (ADRs), numbered ADR-001, ADR-002, ... per project.
CREATE TABLE IF NOT EXISTS decisions (
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    id              TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed', 'accepted', 'rejected', 'superseded')),
    context         TEXT NOT NULL DEFAULT '',
    decision        TEXT NOT NULL DEFAULT '',
    consequences    TEXT NOT NULL DEFAULT '',
    alternatives    JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [{name, branch?, summary, pros, cons}]
    chosen          TEXT NOT NULL DEFAULT '',            -- alternative name or branch
    requirement_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    node_ids        JSONB NOT NULL DEFAULT '[]'::jsonb,
    evaluation      JSONB,
    author          TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, id),
    UNIQUE (project_id, seq)
);
