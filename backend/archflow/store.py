"""Persistence for orgs, projects, branches, versions and the activity log.

Each branch holds its current working state as a JSON document. Versions are immutable
snapshots. Two interchangeable backends share one public interface:

* ``Store`` (this module): SQLite, the zero-setup dev default.
* ``PostgresStore`` (store_pg.py): production, selected when DATABASE_URL is a postgres URL.

Every project belongs to an org (legacy data lives in "org-default"). People are real accounts (``users``)
with browser ``sessions``; org access is a ``memberships`` row (org, user, role). See users.py / authz.py.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import ProjectState

DEFAULT_ORG_ID = "org-default"
DEFAULT_ORG_NAME = "Default"

SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,      -- stored lower-cased
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,     -- scrypt, see users.py
    created_at TEXT NOT NULL,
    last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,     -- SHA-256 of the cookie value; the token itself is never stored
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    user_agent TEXT NOT NULL DEFAULT ''
);
{MEMBERSHIPS}
CREATE TABLE IF NOT EXISTS invites (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    email TEXT,                      -- optional: only this address may accept
    role TEXT NOT NULL,
    created_by TEXT,
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL,
    accepted_at TEXT,
    accepted_by TEXT,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS org_ai_keys (
    org_id TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,          -- gemini | anthropic | openai
    ciphertext TEXT NOT NULL,        -- AES-GCM, see secrets_box.py
    last4 TEXT NOT NULL,
    updated_by TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (org_id, provider)
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    org_id TEXT NOT NULL DEFAULT 'org-default' REFERENCES orgs(id)
);
CREATE TABLE IF NOT EXISTS branches (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    state_json TEXT NOT NULL,
    base_version INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, name)
);
CREATE TABLE IF NOT EXISTS versions (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    branch TEXT NOT NULL,
    message TEXT NOT NULL,
    author TEXT NOT NULL,
    parent INTEGER,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, number)
);
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS api_tokens (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh', 'pat')),
    org_id TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    user_name TEXT NOT NULL,         -- display name at creation (for listings)
    user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
    scopes TEXT NOT NULL,
    client_id TEXT,
    name TEXT NOT NULL DEFAULT '',
    resource TEXT,
    created_at TEXT NOT NULL,
    expires_at REAL,
    last_used_at TEXT,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_api_tokens_org ON api_tokens (org_id, kind);
CREATE TABLE IF NOT EXISTS ydocs (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    branch TEXT NOT NULL,
    data BLOB NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, branch)
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    branch TEXT NOT NULL,
    actor TEXT NOT NULL,
    summary TEXT NOT NULL,
    rev INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    id TEXT NOT NULL,                -- ADR-001, ADR-002, ... (sequential per project)
    seq INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'accepted', 'rejected', 'superseded')),
    context TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT '',
    consequences TEXT NOT NULL DEFAULT '',
    alternatives TEXT NOT NULL DEFAULT '[]',     -- JSON [{name, branch?, summary, pros, cons}]
    chosen TEXT NOT NULL DEFAULT '',             -- alternative name or branch
    requirement_ids TEXT NOT NULL DEFAULT '[]',  -- JSON
    node_ids TEXT NOT NULL DEFAULT '[]',         -- JSON
    evaluation TEXT,                             -- JSON, nullable
    author TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, id),
    UNIQUE (project_id, seq)
);
CREATE TABLE IF NOT EXISTS code_graphs (
    id TEXT PRIMARY KEY,             -- cg-<hex>
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source TEXT NOT NULL,            -- git | local | upload
    repo_url TEXT,
    commit_sha TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    summary TEXT NOT NULL DEFAULT '{}',  -- JSON, see codegraph/normalize.summarize
    graph BLOB                       -- zlib-compressed normalized graph JSON (codegraph/__init__.py)
);
"""

MEMBERSHIPS_DDL = """
CREATE TABLE IF NOT EXISTS memberships (
    org_id TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (org_id, user_id)
);
"""
SCHEMA = SCHEMA.replace("{MEMBERSHIPS}", MEMBERSHIPS_DDL)

# Indexes are created after in-place migrations, since they may reference migrated columns.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_projects_org ON projects(org_id);
CREATE INDEX IF NOT EXISTS idx_versions_branch ON versions(project_id, branch);
CREATE INDEX IF NOT EXISTS idx_activity_branch ON activity(project_id, branch, id DESC);
CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_invites_org ON invites(org_id);
CREATE INDEX IF NOT EXISTS idx_code_graphs_project ON code_graphs(project_id, created_at);
"""

USER_COLS = "id, email, name, created_at, last_login_at"
INVITE_COLS = "id, org_id, email, role, created_by, created_at, expires_at, accepted_at, accepted_by, revoked"

PROJECT_COLS = "id, name, description, created_at, org_id"

# ---- architecture decision records (shared by both stores) ----
DECISION_STATUSES = ("proposed", "accepted", "rejected", "superseded")
DECISION_FIELDS = ("title", "status", "context", "decision", "consequences", "alternatives", "chosen",
                   "requirement_ids", "node_ids", "evaluation")
DECISION_JSON_FIELDS = ("alternatives", "requirement_ids", "node_ids", "evaluation")
DECISION_COLS = ("id, title, status, context, decision, consequences, alternatives, chosen, requirement_ids, "
                 "node_ids, evaluation, author, created_at, updated_at")


# ---- code graphs (repository analysis, shared by both stores) ----
CODE_GRAPH_COLS = "id, project_id, source, repo_url, commit_sha, created_at, created_by, status, summary"


def code_graph_id() -> str:
    return f"cg-{secrets.token_hex(6)}"


def decision_id(seq: int) -> str:
    return f"ADR-{seq:03d}"


def check_decision_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Keep only updatable decision fields and validate status; raises ValueError."""
    out = {k: v for k, v in fields.items() if k in DECISION_FIELDS}
    if "status" in out and out["status"] not in DECISION_STATUSES:
        raise ValueError(f"status must be one of {', '.join(DECISION_STATUSES)}")
    if "title" in out and not str(out["title"] or "").strip():
        raise ValueError("title must not be empty")
    for k in ("alternatives", "requirement_ids", "node_ids"):
        if k in out and out[k] is None:
            out[k] = []
    return out


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        # One connection shared across threads: every statement runs under this lock, and callers
        # also hold it around read-modify-write cycles on branch state.
        self.lock = threading.RLock()
        with self.lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.executescript(INDEXES)

    def _migrate(self) -> None:
        """In-place upgrades for databases created by older versions."""
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO orgs (id, name, created_at) VALUES (?,?,?)",
                (DEFAULT_ORG_ID, DEFAULT_ORG_NAME, now()),
            )
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(projects)")}
            if "org_id" not in cols:
                # SQLite cannot add a REFERENCES column with a non-NULL default while foreign keys are
                # on, so migrated DBs get the column without the FK (fresh DBs have it).
                self._conn.execute(
                    f"ALTER TABLE projects ADD COLUMN org_id TEXT NOT NULL DEFAULT '{DEFAULT_ORG_ID}'"
                )
            # Accounts (005): memberships used to be keyed by a free-form display name, which cannot be mapped
            # to a real account. Those rows are dropped; the first person to sign up becomes owner of every org.
            mcols = {r["name"] for r in self._conn.execute("PRAGMA table_info(memberships)")}
            if "user_name" in mcols:
                self._conn.execute("DROP TABLE memberships")
                self._conn.executescript(MEMBERSHIPS_DDL)
            tcols = {r["name"] for r in self._conn.execute("PRAGMA table_info(api_tokens)")}
            if "user_id" not in tcols:
                self._conn.execute("ALTER TABLE api_tokens ADD COLUMN user_id TEXT REFERENCES users(id) ON DELETE CASCADE")
            # Tokens issued before accounts existed belong to nobody: they stop working.
            self._conn.execute("UPDATE api_tokens SET revoked=1 WHERE user_id IS NULL AND revoked=0")

    def _one(self, sql: str, params: tuple = ()):
        with self.lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()):
        with self.lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self.lock:
            self._conn.close()

    @contextmanager
    def transaction(self, pid: str, branch: str) -> Iterator[ProjectState | None]:
        """Serialized read-modify-write on one branch; yields its current state (None if missing).

        SQLite is single-process here, so the in-process lock is sufficient.
        """
        with self.lock:
            yield self.get_state(pid, branch)

    # ---- orgs -----------------------------------------------------------
    def create_org(self, name: str) -> dict[str, Any]:
        oid = f"org-{secrets.token_hex(4)}"
        with self.lock, self._conn:
            self._conn.execute("INSERT INTO orgs (id, name, created_at) VALUES (?,?,?)", (oid, name, now()))
        return self.get_org(oid)

    def list_orgs(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._all("SELECT id, name, created_at FROM orgs ORDER BY created_at, id")]

    def get_org(self, org_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT id, name, created_at FROM orgs WHERE id=?", (org_id,))
        return dict(row) if row else None

    def update_org(self, org_id: str, name: str) -> dict[str, Any] | None:
        with self.lock, self._conn:
            self._conn.execute("UPDATE orgs SET name=? WHERE id=?", (name, org_id))
        return self.get_org(org_id)

    # ---- memberships ------------------------------------------------------
    def add_member(self, org_id: str, user_id: str, role: str) -> dict[str, Any]:
        """Add a member or change their role (upsert)."""
        with self.lock, self._conn:
            self._conn.execute(
                "INSERT INTO memberships (org_id, user_id, role, created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(org_id, user_id) DO UPDATE SET role=excluded.role",
                (org_id, user_id, role, now()),
            )
        return self.get_member(org_id, user_id)

    def get_member(self, org_id: str, user_id: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT m.org_id, m.user_id, u.email, u.name, m.role, m.created_at FROM memberships m "
            "JOIN users u ON u.id = m.user_id WHERE m.org_id=? AND m.user_id=?", (org_id, user_id))
        return dict(row) if row else None

    def get_role(self, org_id: str, user_id: str) -> str | None:
        row = self._one("SELECT role FROM memberships WHERE org_id=? AND user_id=?", (org_id, user_id))
        return row["role"] if row else None

    def list_members(self, org_id: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT m.org_id, m.user_id, u.email, u.name, m.role, m.created_at FROM memberships m "
            "JOIN users u ON u.id = m.user_id WHERE m.org_id=? ORDER BY m.created_at, u.email", (org_id,))
        return [dict(r) for r in rows]

    def remove_member(self, org_id: str, user_id: str) -> bool:
        with self.lock, self._conn:
            cur = self._conn.execute("DELETE FROM memberships WHERE org_id=? AND user_id=?", (org_id, user_id))
        return cur.rowcount > 0

    def count_role(self, org_id: str, role: str) -> int:
        return self._one("SELECT COUNT(*) FROM memberships WHERE org_id=? AND role=?", (org_id, role))[0]

    def list_user_orgs(self, user_id: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT o.id, o.name, o.created_at, m.role FROM memberships m JOIN orgs o ON o.id = m.org_id "
            "WHERE m.user_id=? ORDER BY o.created_at, o.id", (user_id,))
        return [dict(r) for r in rows]

    # ---- users and sessions -------------------------------------------------
    def count_users(self) -> int:
        return self._one("SELECT COUNT(*) FROM users")[0]

    def create_user(self, email: str, name: str, password_hash: str) -> dict[str, Any]:
        uid = f"u-{secrets.token_hex(6)}"
        with self.lock, self._conn:
            self._conn.execute("INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?,?,?,?,?)",
                               (uid, email, name, password_hash, now()))
        return self.get_user(uid)

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        row = self._one(f"SELECT {USER_COLS} FROM users WHERE id=?", (user_id,))
        return dict(row) if row else None

    def get_user_by_email(self, email: str, with_hash: bool = False) -> dict[str, Any] | None:
        cols = USER_COLS + (", password_hash" if with_hash else "")
        row = self._one(f"SELECT {cols} FROM users WHERE email=?", (email,))
        return dict(row) if row else None

    def touch_login(self, user_id: str) -> None:
        with self.lock, self._conn:
            self._conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (now(), user_id))

    def create_session(self, token_hash: str, user_id: str, expires_at: float, user_agent: str = "") -> None:
        with self.lock, self._conn:
            self._conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, user_agent) VALUES (?,?,?,?,?)",
                (token_hash, user_id, now(), expires_at, user_agent[:300]))

    def get_session(self, token_hash: str) -> dict[str, Any] | None:
        """Active session (not revoked, not expired)."""
        row = self._one("SELECT token_hash, user_id, created_at, expires_at, user_agent FROM sessions "
                        "WHERE token_hash=? AND revoked=0 AND expires_at >= ?", (token_hash, time.time()))
        return dict(row) if row else None

    def extend_session(self, token_hash: str, expires_at: float) -> None:
        with self.lock, self._conn:
            self._conn.execute("UPDATE sessions SET expires_at=? WHERE token_hash=?", (expires_at, token_hash))

    def revoke_session(self, token_hash: str) -> None:
        with self.lock, self._conn:
            self._conn.execute("UPDATE sessions SET revoked=1 WHERE token_hash=?", (token_hash,))

    # ---- invites --------------------------------------------------------------
    @staticmethod
    def _invite(row) -> dict[str, Any] | None:
        if not row:
            return None
        d = dict(row)
        d["revoked"] = bool(d["revoked"])
        return d

    def create_invite(self, org_id: str, token_hash: str, role: str, expires_at: float, email: str | None = None,
                      created_by: str | None = None) -> dict[str, Any]:
        iid = f"inv-{secrets.token_hex(6)}"
        with self.lock, self._conn:
            self._conn.execute(
                "INSERT INTO invites (id, org_id, token_hash, email, role, created_by, created_at, expires_at) "
                "VALUES (?,?,?,?,?,?,?,?)", (iid, org_id, token_hash, email, role, created_by, now(), expires_at))
        return self._invite(self._one(f"SELECT {INVITE_COLS} FROM invites WHERE id=?", (iid,)))

    def get_invite(self, token_hash: str) -> dict[str, Any] | None:
        """Usable invite by hash (not revoked, not accepted, not expired)."""
        return self._invite(self._one(
            f"SELECT {INVITE_COLS} FROM invites WHERE token_hash=? AND revoked=0 AND accepted_at IS NULL "
            "AND expires_at >= ?", (token_hash, time.time())))

    def list_invites(self, org_id: str) -> list[dict[str, Any]]:
        rows = self._all(f"SELECT {INVITE_COLS} FROM invites WHERE org_id=? AND revoked=0 AND accepted_at IS NULL "
                         "AND expires_at >= ? ORDER BY created_at DESC", (org_id, time.time()))
        return [self._invite(r) for r in rows]

    def revoke_invite(self, org_id: str, invite_id: str) -> bool:
        with self.lock, self._conn:
            cur = self._conn.execute("UPDATE invites SET revoked=1 WHERE id=? AND org_id=? AND revoked=0 "
                                     "AND accepted_at IS NULL", (invite_id, org_id))
        return cur.rowcount > 0

    def accept_invite(self, invite_id: str, user_id: str) -> bool:
        """Mark an invite used (single use, atomic). False if it was already used, revoked or expired."""
        with self.lock, self._conn:
            cur = self._conn.execute(
                "UPDATE invites SET accepted_at=?, accepted_by=? WHERE id=? AND accepted_at IS NULL AND revoked=0 "
                "AND expires_at >= ?", (now(), user_id, invite_id, time.time()))
        return cur.rowcount > 0

    # ---- per-org AI provider keys (encrypted by the caller, see secrets_box.py) ----------
    def set_ai_key(self, org_id: str, provider: str, ciphertext: str, last4: str, user_id: str | None) -> None:
        with self.lock, self._conn:
            self._conn.execute(
                "INSERT INTO org_ai_keys (org_id, provider, ciphertext, last4, updated_by, updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(org_id, provider) DO UPDATE SET ciphertext=excluded.ciphertext, "
                "last4=excluded.last4, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (org_id, provider, ciphertext, last4, user_id, now()))

    def delete_ai_key(self, org_id: str, provider: str) -> None:
        with self.lock, self._conn:
            self._conn.execute("DELETE FROM org_ai_keys WHERE org_id=? AND provider=?", (org_id, provider))

    def list_ai_keys(self, org_id: str) -> list[dict[str, Any]]:
        rows = self._all("SELECT provider, ciphertext, last4, updated_by, updated_at FROM org_ai_keys "
                         "WHERE org_id=? ORDER BY provider", (org_id,))
        return [dict(r) for r in rows]

    # ---- projects -------------------------------------------------------
    def create_project(self, name: str, description: str = "", state: ProjectState | None = None,
                       org_id: str = DEFAULT_ORG_ID) -> dict[str, Any]:
        pid = f"p-{secrets.token_hex(4)}"
        ts = now()
        with self.lock, self._conn:
            self._conn.execute(
                f"INSERT INTO projects ({PROJECT_COLS}) VALUES (?,?,?,?,?)", (pid, name, description, ts, org_id)
            )
            self._conn.execute(
                "INSERT INTO branches (project_id, name, state_json, base_version, created_at) VALUES (?,?,?,?,?)",
                (pid, "main", (state or ProjectState()).model_dump_json(), None, ts),
            )
        return self.get_project(pid)

    def list_projects(self, org_id: str | None = None) -> list[dict[str, Any]]:
        if org_id is None:
            rows = self._all(f"SELECT {PROJECT_COLS} FROM projects ORDER BY created_at DESC")
        else:
            rows = self._all(
                f"SELECT {PROJECT_COLS} FROM projects WHERE org_id=? ORDER BY created_at DESC", (org_id,)
            )
        return [dict(r) for r in rows]

    def get_project(self, pid: str) -> dict[str, Any] | None:
        row = self._one(f"SELECT {PROJECT_COLS} FROM projects WHERE id=?", (pid,))
        return dict(row) if row else None

    def delete_project(self, pid: str) -> None:
        with self.lock, self._conn:
            self._conn.execute("DELETE FROM projects WHERE id=?", (pid,))

    # ---- branches -------------------------------------------------------
    def list_branches(self, pid: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT name, base_version, created_at FROM branches WHERE project_id=? ORDER BY created_at", (pid,)
        )
        return [dict(r) for r in rows]

    def get_state(self, pid: str, branch: str) -> ProjectState | None:
        row = self._one(
            "SELECT state_json FROM branches WHERE project_id=? AND name=?", (pid, branch)
        )
        return ProjectState.model_validate_json(row["state_json"]) if row else None

    def save_state(self, pid: str, branch: str, state: ProjectState) -> None:
        with self.lock, self._conn:
            self._conn.execute(
                "UPDATE branches SET state_json=? WHERE project_id=? AND name=?",
                (state.model_dump_json(), pid, branch),
            )

    def create_branch(self, pid: str, name: str, state: ProjectState, base_version: int | None) -> None:
        with self.lock, self._conn:
            self._conn.execute(
                "INSERT INTO branches (project_id, name, state_json, base_version, created_at) VALUES (?,?,?,?,?)",
                (pid, name, state.model_dump_json(), base_version, now()),
            )

    # ---- versions -------------------------------------------------------
    def commit(self, pid: str, branch: str, message: str, author: str, state: ProjectState) -> dict[str, Any]:
        with self.lock, self._conn:
            number = self._conn.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 FROM versions WHERE project_id=?", (pid,)
            ).fetchone()[0]
            parent = self.head_version(pid, branch)
            if parent is None:
                row = self._one(
                    "SELECT base_version FROM branches WHERE project_id=? AND name=?", (pid, branch)
                )
                parent = row["base_version"] if row else None
            self._conn.execute(
                "INSERT INTO versions (project_id, number, branch, message, author, parent, state_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (pid, number, branch, message, author, parent, state.model_dump_json(), now()),
            )
        return self.get_version(pid, number, with_state=False)

    def head_version(self, pid: str, branch: str) -> int | None:
        row = self._one(
            "SELECT MAX(number) FROM versions WHERE project_id=? AND branch=?", (pid, branch)
        )
        return row[0]

    def list_versions(self, pid: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT number, branch, message, author, parent, created_at FROM versions "
            "WHERE project_id=? ORDER BY number DESC", (pid,)
        )
        return [dict(r) for r in rows]

    def get_version(self, pid: str, number: int, with_state: bool = True) -> dict[str, Any] | None:
        row = self._one(
            "SELECT project_id, number, branch, message, author, parent, state_json, created_at "
            "FROM versions WHERE project_id=? AND number=?", (pid, number)
        )
        if not row:
            return None
        out = dict(row)
        state_json = out.pop("state_json")
        if with_state:
            out["state"] = ProjectState.model_validate_json(state_json)
        return out

    # ---- activity -------------------------------------------------------
    def log_activity(self, pid: str, branch: str, actor: str, summary: list[str], rev: int) -> None:
        if not summary:
            return
        with self.lock, self._conn:
            self._conn.execute(
                "INSERT INTO activity (project_id, branch, actor, summary, rev, created_at) VALUES (?,?,?,?,?,?)",
                (pid, branch, actor, json.dumps(summary), rev, now()),
            )

    # ---- collaborative documents (binary Yjs state) -----------------------
    def get_ydoc(self, pid: str, branch: str) -> bytes | None:
        row = self._one("SELECT data FROM ydocs WHERE project_id=? AND branch=?", (pid, branch))
        return bytes(row["data"]) if row else None

    def put_ydoc(self, pid: str, branch: str, data: bytes) -> None:
        with self.lock, self._conn:
            self._conn.execute(
                "INSERT INTO ydocs (project_id, branch, data, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(project_id, branch) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (pid, branch, data, now()),
            )

    def delete_ydoc(self, pid: str, branch: str) -> None:
        with self.lock, self._conn:
            self._conn.execute("DELETE FROM ydocs WHERE project_id=? AND branch=?", (pid, branch))

    # ---- OAuth clients, authorization codes and API tokens ----------------------
    # Secrets are never stored: codes and tokens are kept as SHA-256 hashes (see auth.py).
    def save_oauth_client(self, client_id: str, data: dict[str, Any]) -> None:
        with self.lock, self._conn:
            self._conn.execute(
                "INSERT INTO oauth_clients (client_id, data, created_at) VALUES (?,?,?) "
                "ON CONFLICT(client_id) DO UPDATE SET data=excluded.data", (client_id, json.dumps(data), now()))

    def get_oauth_client(self, client_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT data FROM oauth_clients WHERE client_id=?", (client_id,))
        return json.loads(row["data"]) if row else None

    def save_auth_code(self, code_hash: str, data: dict[str, Any], expires_at: float) -> None:
        with self.lock, self._conn:
            self._conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (time.time(),))
            self._conn.execute("INSERT OR REPLACE INTO oauth_codes (code_hash, data, expires_at) VALUES (?,?,?)",
                               (code_hash, json.dumps(data), expires_at))

    def get_auth_code(self, code_hash: str) -> dict[str, Any] | None:
        row = self._one("SELECT data FROM oauth_codes WHERE code_hash=? AND expires_at >= ?", (code_hash, time.time()))
        return json.loads(row["data"]) if row else None

    def pop_auth_code(self, code_hash: str) -> dict[str, Any] | None:
        """Single use: returns the code's data and deletes it (None if missing or expired)."""
        with self.lock, self._conn:
            row = self._conn.execute("SELECT data, expires_at FROM oauth_codes WHERE code_hash=?", (code_hash,)).fetchone()
            self._conn.execute("DELETE FROM oauth_codes WHERE code_hash=?", (code_hash,))
        if not row or row["expires_at"] < time.time():
            return None
        return json.loads(row["data"])

    def create_token(self, token_hash: str, kind: str, org_id: str, user_name: str, scopes: list[str],
                     client_id: str | None = None, name: str = "", expires_at: float | None = None,
                     resource: str | None = None, user_id: str | None = None) -> dict[str, Any]:
        tid = f"tok-{secrets.token_hex(6)}"
        with self.lock, self._conn:
            self._conn.execute(
                "INSERT INTO api_tokens (id, token_hash, kind, org_id, user_name, user_id, scopes, client_id, name, "
                "resource, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, token_hash, kind, org_id, user_name, user_id, json.dumps(scopes), client_id, name, resource,
                 now(), expires_at))
        return self.get_token_by_id(tid)

    @staticmethod
    def _token(row) -> dict[str, Any] | None:
        if not row:
            return None
        d = dict(row)
        d.pop("token_hash", None)
        d["scopes"] = json.loads(d["scopes"])
        d["revoked"] = bool(d["revoked"])
        return d

    def get_token(self, token_hash: str) -> dict[str, Any] | None:
        """Active token by hash (not revoked, not expired)."""
        row = self._one("SELECT * FROM api_tokens WHERE token_hash=? AND revoked=0 AND (expires_at IS NULL OR expires_at >= ?)",
                        (token_hash, time.time()))
        return self._token(row)

    def get_token_by_id(self, token_id: str) -> dict[str, Any] | None:
        return self._token(self._one("SELECT * FROM api_tokens WHERE id=?", (token_id,)))

    def touch_token(self, token_hash: str) -> None:
        with self.lock, self._conn:
            self._conn.execute("UPDATE api_tokens SET last_used_at=? WHERE token_hash=?", (now(), token_hash))

    def revoke_token(self, token_hash: str) -> None:
        with self.lock, self._conn:
            self._conn.execute("UPDATE api_tokens SET revoked=1 WHERE token_hash=?", (token_hash,))

    def revoke_token_id(self, token_id: str) -> bool:
        with self.lock, self._conn:
            cur = self._conn.execute("UPDATE api_tokens SET revoked=1 WHERE id=? AND revoked=0", (token_id,))
        return cur.rowcount > 0

    def revoke_user_tokens(self, org_id: str, user_id: str) -> None:
        with self.lock, self._conn:
            self._conn.execute("UPDATE api_tokens SET revoked=1 WHERE org_id=? AND user_id=?", (org_id, user_id))

    def list_tokens(self, org_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM api_tokens WHERE org_id=? AND revoked=0"
        params: tuple = (org_id,)
        if kind:
            sql += " AND kind=?"
            params += (kind,)
        return [self._token(r) for r in self._all(sql + " ORDER BY created_at DESC", params)]

    def list_activity(self, pid: str, branch: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT actor, summary, rev, created_at FROM activity WHERE project_id=? AND branch=? "
            "ORDER BY id DESC LIMIT ?", (pid, branch, limit)
        )
        return [{**dict(r), "summary": json.loads(r["summary"])} for r in rows]

    # ---- architecture decision records ----------------------------------
    @staticmethod
    def _decision(row) -> dict[str, Any] | None:
        if not row:
            return None
        d = dict(row)
        for k in DECISION_JSON_FIELDS:
            d[k] = json.loads(d[k]) if d[k] is not None else None
        return d

    def create_decision(self, pid: str, title: str, *, author: str = "anonymous", **fields: Any) -> dict[str, Any]:
        data = check_decision_fields({"title": title, **fields})
        data.setdefault("status", "proposed")
        ts = now()
        with self.lock, self._conn:
            seq = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM decisions WHERE project_id=?", (pid,)).fetchone()[0]
            did = decision_id(seq)
            self._conn.execute(
                "INSERT INTO decisions (project_id, id, seq, title, status, context, decision, consequences, "
                "alternatives, chosen, requirement_ids, node_ids, evaluation, author, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, did, seq, data["title"].strip(), data["status"], data.get("context", ""),
                 data.get("decision", ""), data.get("consequences", ""), json.dumps(data.get("alternatives", [])),
                 data.get("chosen", ""), json.dumps(data.get("requirement_ids", [])),
                 json.dumps(data.get("node_ids", [])),
                 json.dumps(data["evaluation"]) if data.get("evaluation") is not None else None, author, ts, ts),
            )
        return self.get_decision(pid, did)

    def list_decisions(self, pid: str) -> list[dict[str, Any]]:
        rows = self._all(f"SELECT {DECISION_COLS} FROM decisions WHERE project_id=? ORDER BY seq", (pid,))
        return [self._decision(r) for r in rows]

    def get_decision(self, pid: str, did: str) -> dict[str, Any] | None:
        return self._decision(self._one(
            f"SELECT {DECISION_COLS} FROM decisions WHERE project_id=? AND id=?", (pid, did)))

    def update_decision(self, pid: str, did: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        """Update the given fields (see DECISION_FIELDS); returns the decision, or None if it does not exist."""
        data = check_decision_fields(fields)
        if data:
            sets, params = [], []
            for k, v in data.items():
                sets.append(f"{k}=?")
                params.append(json.dumps(v) if k in DECISION_JSON_FIELDS and v is not None else v)
            with self.lock, self._conn:
                self._conn.execute(
                    f"UPDATE decisions SET {', '.join(sets)}, updated_at=? WHERE project_id=? AND id=?",
                    (*params, now(), pid, did))
        return self.get_decision(pid, did)

    # ---- code graphs (repository analysis) ------------------------------------
    def _code_graph(self, row, with_graph: bool = False) -> dict[str, Any] | None:
        if not row:
            return None
        d = dict(row)
        d["summary"] = json.loads(d["summary"] or "{}")
        if with_graph:
            d["graph"] = bytes(d["graph"]) if d.get("graph") is not None else None
        else:
            d.pop("graph", None)
        return d

    def add_code_graph(self, pid: str, *, source: str, repo_url: str | None, commit_sha: str | None,
                       created_by: str, status: str = "ready", summary: dict[str, Any] | None = None,
                       graph: bytes | None = None) -> dict[str, Any]:
        gid = code_graph_id()
        with self.lock, self._conn:
            self._conn.execute(
                f"INSERT INTO code_graphs ({CODE_GRAPH_COLS}, graph) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (gid, pid, source, repo_url, commit_sha, now(), created_by, status, json.dumps(summary or {}), graph))
        return self.get_code_graph(pid, gid)

    def get_code_graph(self, pid: str, gid: str, with_graph: bool = False) -> dict[str, Any] | None:
        cols = CODE_GRAPH_COLS + (", graph" if with_graph else "")
        return self._code_graph(self._one(f"SELECT {cols} FROM code_graphs WHERE project_id=? AND id=?", (pid, gid)),
                                with_graph)

    def list_code_graphs(self, pid: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._all(f"SELECT {CODE_GRAPH_COLS} FROM code_graphs WHERE project_id=? "
                         "ORDER BY created_at DESC, rowid DESC LIMIT ?", (pid, limit))
        return [self._code_graph(r) for r in rows]

    def latest_code_graph(self, pid: str, with_graph: bool = False) -> dict[str, Any] | None:
        cols = CODE_GRAPH_COLS + (", graph" if with_graph else "")
        return self._code_graph(self._one(f"SELECT {cols} FROM code_graphs WHERE project_id=? AND status='ready' "
                                          "ORDER BY created_at DESC, rowid DESC LIMIT 1", (pid,)), with_graph)


def default_store():
    """Postgres when DATABASE_URL is a postgres URL, otherwise SQLite (ARCHFLOW_DB or backend/data/archflow.db)."""
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith(("postgres://", "postgresql://")):
        from .store_pg import PostgresStore

        return PostgresStore(url)
    return Store(os.environ.get("ARCHFLOW_DB", Path(__file__).resolve().parent.parent / "data" / "archflow.db"))
