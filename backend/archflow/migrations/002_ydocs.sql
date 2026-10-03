-- 002_ydocs: binary Yjs document state per project branch (real-time collaboration).
CREATE TABLE IF NOT EXISTS ydocs (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    branch TEXT NOT NULL,
    data BYTEA NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, branch)
);
