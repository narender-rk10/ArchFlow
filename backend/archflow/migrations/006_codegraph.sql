-- 006_codegraph: repository analyses (normalized code graphs, see archflow/codegraph/__init__.py).
CREATE TABLE IF NOT EXISTS code_graphs (
    id          TEXT PRIMARY KEY,                 -- cg-<hex>
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,                    -- git | local | upload
    repo_url    TEXT,
    commit_sha  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ready',
    summary     JSONB NOT NULL DEFAULT '{}'::jsonb,
    graph       BYTEA                             -- zlib-compressed normalized graph JSON
);
CREATE INDEX IF NOT EXISTS idx_code_graphs_project ON code_graphs (project_id, created_at DESC);
