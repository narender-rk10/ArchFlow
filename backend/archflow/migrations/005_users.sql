-- 005_users: real accounts, browser sessions, org memberships by user, invites and per-org AI provider keys.
--
-- Migration path: memberships used to be keyed by a free-form display name (user_name), which cannot be mapped
-- to an account. Those rows are dropped. The first person to sign up becomes owner of every existing org
-- (including org-default), so legacy data is claimed by whoever bootstraps the instance. API tokens issued before
-- accounts existed have no owner and are revoked.

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,          -- stored lower-cased
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL,                 -- scrypt (see archflow/users.py)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,                 -- SHA-256 of the cookie value; the token is never stored
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at DOUBLE PRECISION NOT NULL,
    revoked    BOOLEAN NOT NULL DEFAULT FALSE,
    user_agent TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);

DROP TABLE IF EXISTS memberships;
CREATE TABLE memberships (
    org_id     TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships (user_id);

ALTER TABLE api_tokens ADD COLUMN IF NOT EXISTS user_id TEXT REFERENCES users(id) ON DELETE CASCADE;
UPDATE api_tokens SET revoked = TRUE WHERE user_id IS NULL AND NOT revoked;

CREATE TABLE IF NOT EXISTS invites (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    email       TEXT,                            -- optional: only this address may accept
    role        TEXT NOT NULL,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  DOUBLE PRECISION NOT NULL,
    accepted_at TIMESTAMPTZ,
    accepted_by TEXT,
    revoked     BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_invites_org ON invites (org_id);

CREATE TABLE IF NOT EXISTS org_ai_keys (
    org_id     TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    provider   TEXT NOT NULL,                    -- gemini | anthropic | openai
    ciphertext TEXT NOT NULL,                    -- AES-256-GCM (see archflow/secrets_box.py)
    last4      TEXT NOT NULL,
    updated_by TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, provider)
);
