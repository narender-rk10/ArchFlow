-- 009_ai_usage: tokens and estimated cost of every LLM call, and each organization's monthly AI budget.
CREATE TABLE IF NOT EXISTS ai_usage (
    id                BIGSERIAL PRIMARY KEY,
    org_id            TEXT NOT NULL,                  -- no FK: usage history survives deleting an org's projects
    project_id        TEXT NOT NULL DEFAULT '',
    feature           TEXT NOT NULL DEFAULT 'other',  -- assistant, canvas-edit, design-chat, openspec, lld, ...
    model             TEXT NOT NULL DEFAULT '',
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd          DOUBLE PRECISION NOT NULL DEFAULT 0,
    cost_source       TEXT NOT NULL DEFAULT '',       -- litellm | table | estimate | unknown
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_org_time ON ai_usage (org_id, created_at);

CREATE TABLE IF NOT EXISTS org_ai_budgets (
    org_id            TEXT PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
    monthly_limit_usd DOUBLE PRECISION,               -- NULL = no limit
    alert_percent     INTEGER NOT NULL DEFAULT 80,
    updated_by        TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
