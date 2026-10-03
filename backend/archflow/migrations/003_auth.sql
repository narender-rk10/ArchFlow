-- 003_auth: OAuth 2.1 clients/codes and API tokens (OAuth access/refresh tokens and personal access tokens).
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS api_tokens (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh', 'pat')),
    org_id TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    user_name TEXT NOT NULL,
    scopes JSONB NOT NULL,
    client_id TEXT,
    name TEXT NOT NULL DEFAULT '',
    resource TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at DOUBLE PRECISION,
    last_used_at TIMESTAMPTZ,
    revoked BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_api_tokens_org ON api_tokens (org_id, kind);
