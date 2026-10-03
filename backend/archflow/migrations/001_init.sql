-- 001_init: ArchFlow core schema (orgs, memberships, projects, branches, versions, activity).
-- Applied by archflow.store_pg's migration runner; recorded in schema_migrations.

CREATE EXTENSION IF NOT EXISTS vector;  -- pgvector, for embeddings/semantic search later

CREATE TABLE IF NOT EXISTS orgs (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- TODO(auth): user_name is a display name until real user accounts exist; then becomes user_id FK.
CREATE TABLE IF NOT EXISTS memberships (
    org_id      TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    user_name   TEXT NOT NULL,
    role        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, user_name)
);

INSERT INTO orgs (id, name) VALUES ('org-default', 'Default') ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL DEFAULT 'org-default' REFERENCES orgs(id),
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_projects_org ON projects (org_id);

CREATE TABLE IF NOT EXISTS branches (
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    state_json   JSONB NOT NULL,
    base_version INTEGER,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, name)
);

CREATE TABLE IF NOT EXISTS versions (
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    number      INTEGER NOT NULL,
    branch      TEXT NOT NULL,
    message     TEXT NOT NULL,
    author      TEXT NOT NULL,
    parent      INTEGER,
    state_json  JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, number)
);
CREATE INDEX IF NOT EXISTS idx_versions_branch ON versions (project_id, branch);

CREATE TABLE IF NOT EXISTS activity (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    branch      TEXT NOT NULL,
    actor       TEXT NOT NULL,
    summary     JSONB NOT NULL,
    rev         INTEGER NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_activity_branch ON activity (project_id, branch, id DESC);

-- Row-level security scaffolding: isolate projects by the session setting app.org_id.
-- Non-enforcing for now: the policy exists and RLS is enabled, but FORCE is not, so the table owner
-- (the app's DB role) bypasses it, and an unset app.org_id matches everything.
-- TODO(auth): once auth lands, run the app as a non-owner role (or FORCE ROW LEVEL SECURITY), set
-- app.org_id per request/transaction, drop the "unset matches all" clause, and extend to child tables.
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
CREATE POLICY projects_org_isolation ON projects
    USING (
        current_setting('app.org_id', true) IS NULL
        OR current_setting('app.org_id', true) = ''
        OR org_id = current_setting('app.org_id', true)
    );
