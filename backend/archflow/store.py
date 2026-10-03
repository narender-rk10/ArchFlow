"""Persistence for orgs, projects, branches, versions and the activity log.

Each branch holds its current working state as a JSON document. Versions are immutable
snapshots. Two interchangeable backends share one public interface:

* ``Store`` (this module): SQLite, the zero-setup dev default.
* ``PostgresStore`` (store_pg.py): production, selected when DATABASE_URL is a postgres URL.

Multi-tenancy is minimal for now: every project belongs to an org (default "org-default").
TODO(auth): user identity is a free-form display name; replace with real users once auth lands.
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
CREATE TABLE IF NOT EXISTS memberships (
    org_id TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    user_name TEXT NOT NULL,  -- TODO(auth): becomes user_id once real auth exists
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (org_id, user_name)
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
    user_name TEXT NOT NULL,
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
"""

# Indexes are created after in-place migrations, since they may reference migrated columns.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_projects_org ON projects(org_id);
CREATE INDEX IF NOT EXISTS idx_versions_branch ON versions(project_id, branch);
CREATE INDEX IF NOT EXISTS idx_activity_branch ON activity(project_id, branch, id DESC);
"""

PROJECT_COLS = "id, name, description, created_at, org_id"


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

    def add_member(self, org_id: str, user_name: str, role: str = "member") -> dict[str, Any]:
        # TODO(auth): user_name is a display name until real user accounts exist.
        ts = now()
        with self.lock, self._conn:
            self._conn.execute(
                "INSERT INTO memberships (org_id, user_name, role, created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(org_id, user_name) DO UPDATE SET role=excluded.role",
                (org_id, user_name, role, ts),
            )
        row = self._one(
            "SELECT org_id, user_name, role, created_at FROM memberships WHERE org_id=? AND user_name=?",
            (org_id, user_name),
        )
        return dict(row)

    def list_members(self, org_id: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT org_id, user_name, role, created_at FROM memberships WHERE org_id=? ORDER BY created_at, user_name",
            (org_id,),
        )
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
                     resource: str | None = None) -> dict[str, Any]:
        tid = f"tok-{secrets.token_hex(6)}"
        with self.lock, self._conn:
            self._conn.execute(
                "INSERT INTO api_tokens (id, token_hash, kind, org_id, user_name, scopes, client_id, name, resource, "
                "created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (tid, token_hash, kind, org_id, user_name, json.dumps(scopes), client_id, name, resource, now(), expires_at))
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


def default_store():
    """Postgres when DATABASE_URL is a postgres URL, otherwise SQLite (ARCHFLOW_DB or backend/data/archflow.db)."""
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith(("postgres://", "postgresql://")):
        from .store_pg import PostgresStore

        return PostgresStore(url)
    return Store(os.environ.get("ARCHFLOW_DB", Path(__file__).resolve().parent.parent / "data" / "archflow.db"))
