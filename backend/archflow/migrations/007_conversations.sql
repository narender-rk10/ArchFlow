-- 007_conversations: shared AI chat threads with a running summary, and project memory (see archflow/conversations.py).
CREATE TABLE IF NOT EXISTS conversations (
    id              TEXT PRIMARY KEY,                 -- cv-<hex>
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    branch          TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('assistant', 'design')),
    title           TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL DEFAULT '',         -- running summary of turns folded out of the context window
    summarized_seq  INTEGER NOT NULL DEFAULT 0,       -- messages with seq <= this are covered by summary
    created_by      TEXT NOT NULL,
    created_by_id   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conversations_project ON conversations (project_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS conversation_messages (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    mode            TEXT NOT NULL DEFAULT 'ask',      -- ask | command | design | note
    content         TEXT NOT NULL DEFAULT '',
    data            JSONB NOT NULL DEFAULT '{}'::jsonb,
    author          TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (conversation_id, seq)
);

CREATE TABLE IF NOT EXISTS project_memory (
    id              TEXT PRIMARY KEY,                 -- mem-<hex>
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    text            TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'fact',
    pinned          BOOLEAN NOT NULL DEFAULT false,
    source          TEXT NOT NULL DEFAULT 'user',     -- user | ai | design-chat | mcp
    conversation_id TEXT,                             -- no FK: a fact survives deleting the chat it came from
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_project_memory_project ON project_memory (project_id);
