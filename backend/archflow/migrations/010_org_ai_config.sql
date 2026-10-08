-- 010_org_ai_config: an organization's model choices from AI settings (providers + models per group, endpoints).
-- Keys stay encrypted in org_ai_keys; this holds no secrets.
CREATE TABLE IF NOT EXISTS org_ai_config (
    org_id      TEXT PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
    config      JSONB NOT NULL DEFAULT '{}'::jsonb,   -- {"reasoning": [{provider, model}], "fast": [...], "params": {...}}
    updated_by  TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
