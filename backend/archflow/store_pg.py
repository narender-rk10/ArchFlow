"""PostgreSQL persistence with the same public interface as ``archflow.store.Store``.

* psycopg 3 + psycopg_pool.ConnectionPool; JSONB state, TIMESTAMPTZ timestamps.
* Schema lives in ``archflow/migrations/*.sql`` and is applied by a tiny runner that records
  applied files in ``schema_migrations`` (serialized across processes with an advisory lock).
* ``lock`` is an in-process RLock (kept for existing callers). For multi-process safety use
  ``transaction(pid, branch)``, which holds ``SELECT ... FOR UPDATE`` on the branch row; store
  calls made inside it on the same thread run on that transaction's connection.

Returned dicts mirror the SQLite store: timestamps are ISO-8601 strings, states are ProjectState.
"""

from __future__ import annotations

import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .models import ProjectState
from .store import DEFAULT_ORG_ID, DEFAULT_ORG_NAME

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATION_LOCK_KEY = 0x41524346  # "ARCF": pg_advisory_lock key for the migration runner


def _row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: (v.astimezone(timezone.utc).isoformat(timespec="seconds") if isinstance(v, datetime) else v) for k, v in row.items()}


def _state(value: Any) -> ProjectState:
    if isinstance(value, (str, bytes)):
        return ProjectState.model_validate_json(value)
    return ProjectState.model_validate(value)


def _jsonb(state: ProjectState) -> Jsonb:
    return Jsonb(state.model_dump(mode="json"))


def run_migrations(conn: Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending ``NNN_*.sql`` files in order; returns the names applied."""
    applied: list[str] = []
    conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        conn.commit()
        done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
        conn.commit()
        for path in sorted(directory.glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
            applied.append(path.name)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
        conn.commit()
    return applied


class PostgresStore:
    def __init__(self, url: str, min_size: int = 1, max_size: int = 10):
        self.url = url
        self.pool = ConnectionPool(
            url, min_size=min_size, max_size=max_size, kwargs={"row_factory": dict_row}, open=True
        )
        self.lock = threading.RLock()
        self._local = threading.local()  # .conn = connection of the active transaction() on this thread
        with self.pool.connection() as conn:
            run_migrations(conn)
            conn.execute(
                "INSERT INTO orgs (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                (DEFAULT_ORG_ID, DEFAULT_ORG_NAME),
            )

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def _conn(self) -> Iterator[Connection]:
        """The active transaction's connection on this thread, else a pooled one (committed on exit)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            yield conn
            return
        with self.pool.connection() as conn:
            yield conn

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._conn() as conn:
            return conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return conn.execute(sql, params).fetchall()

    @contextmanager
    def transaction(self, pid: str, branch: str) -> Iterator[ProjectState | None]:
        """Read-modify-write on one branch, safe across server processes.

        Locks the branch row (SELECT ... FOR UPDATE) and yields its current state (None if missing).
        Store calls on this thread inside the block (save_state, log_activity, ...) share the same
        transaction, which commits on normal exit and rolls back on exception.
        """
        if getattr(self._local, "conn", None) is not None:
            raise RuntimeError("PostgresStore.transaction() is not re-entrant")
        with self.lock, self.pool.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT state_json FROM branches WHERE project_id=%s AND name=%s FOR UPDATE", (pid, branch)
                ).fetchone()
                self._local.conn = conn
                try:
                    yield _state(row["state_json"]) if row else None
                finally:
                    self._local.conn = None

    # ---- orgs -----------------------------------------------------------
    def create_org(self, name: str) -> dict[str, Any]:
        oid = f"org-{secrets.token_hex(4)}"
        with self._conn() as conn:
            conn.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (oid, name))
        return self.get_org(oid)

    def list_orgs(self) -> list[dict[str, Any]]:
        return [_row(r) for r in self._all("SELECT id, name, created_at FROM orgs ORDER BY created_at, id")]

    def get_org(self, org_id: str) -> dict[str, Any] | None:
        return _row(self._one("SELECT id, name, created_at FROM orgs WHERE id=%s", (org_id,)))

    def add_member(self, org_id: str, user_name: str, role: str = "member") -> dict[str, Any]:
        # TODO(auth): user_name is a display name until real user accounts exist.
        row = self._one(
            "INSERT INTO memberships (org_id, user_name, role) VALUES (%s, %s, %s) "
            "ON CONFLICT (org_id, user_name) DO UPDATE SET role = EXCLUDED.role "
            "RETURNING org_id, user_name, role, created_at",
            (org_id, user_name, role),
        )
        return _row(row)

    def list_members(self, org_id: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT org_id, user_name, role, created_at FROM memberships WHERE org_id=%s "
            "ORDER BY created_at, user_name", (org_id,)
        )
        return [_row(r) for r in rows]

    # ---- projects -------------------------------------------------------
    _PROJECT_COLS = "id, name, description, created_at, org_id"

    def create_project(self, name: str, description: str = "", state: ProjectState | None = None,
                       org_id: str = DEFAULT_ORG_ID) -> dict[str, Any]:
        pid = f"p-{secrets.token_hex(4)}"
        with self.lock, self._conn() as conn, conn.transaction():
            conn.execute(
                "INSERT INTO projects (id, org_id, name, description) VALUES (%s, %s, %s, %s)",
                (pid, org_id, name, description),
            )
            conn.execute(
                "INSERT INTO branches (project_id, name, state_json, base_version, created_at) "
                "SELECT %s, 'main', %s, NULL, created_at FROM projects WHERE id=%s",
                (pid, _jsonb(state or ProjectState()), pid),
            )
        return self.get_project(pid)

    def list_projects(self, org_id: str | None = None) -> list[dict[str, Any]]:
        if org_id is None:
            rows = self._all(f"SELECT {self._PROJECT_COLS} FROM projects ORDER BY created_at DESC")
        else:
            rows = self._all(
                f"SELECT {self._PROJECT_COLS} FROM projects WHERE org_id=%s ORDER BY created_at DESC", (org_id,)
            )
        return [_row(r) for r in rows]

    def get_project(self, pid: str) -> dict[str, Any] | None:
        return _row(self._one(f"SELECT {self._PROJECT_COLS} FROM projects WHERE id=%s", (pid,)))

    def delete_project(self, pid: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM projects WHERE id=%s", (pid,))

    # ---- branches -------------------------------------------------------
    def list_branches(self, pid: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT name, base_version, created_at FROM branches WHERE project_id=%s ORDER BY created_at, name",
            (pid,),
        )
        return [_row(r) for r in rows]

    def get_state(self, pid: str, branch: str) -> ProjectState | None:
        row = self._one("SELECT state_json FROM branches WHERE project_id=%s AND name=%s", (pid, branch))
        return _state(row["state_json"]) if row else None

    def save_state(self, pid: str, branch: str, state: ProjectState) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE branches SET state_json=%s WHERE project_id=%s AND name=%s", (_jsonb(state), pid, branch)
            )

    def create_branch(self, pid: str, name: str, state: ProjectState, base_version: int | None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO branches (project_id, name, state_json, base_version) VALUES (%s, %s, %s, %s)",
                (pid, name, _jsonb(state), base_version),
            )

    # ---- versions -------------------------------------------------------
    def commit(self, pid: str, branch: str, message: str, author: str, state: ProjectState) -> dict[str, Any]:
        with self.lock, self._conn() as conn, conn.transaction():
            # Serialize version numbering per project across processes.
            conn.execute("SELECT 1 FROM projects WHERE id=%s FOR UPDATE", (pid,))
            number = conn.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 AS n FROM versions WHERE project_id=%s", (pid,)
            ).fetchone()["n"]
            parent = conn.execute(
                "SELECT MAX(number) AS n FROM versions WHERE project_id=%s AND branch=%s", (pid, branch)
            ).fetchone()["n"]
            if parent is None:
                row = conn.execute(
                    "SELECT base_version FROM branches WHERE project_id=%s AND name=%s", (pid, branch)
                ).fetchone()
                parent = row["base_version"] if row else None
            conn.execute(
                "INSERT INTO versions (project_id, number, branch, message, author, parent, state_json) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (pid, number, branch, message, author, parent, _jsonb(state)),
            )
        return self.get_version(pid, number, with_state=False)

    def head_version(self, pid: str, branch: str) -> int | None:
        row = self._one(
            "SELECT MAX(number) AS n FROM versions WHERE project_id=%s AND branch=%s", (pid, branch)
        )
        return row["n"]

    def list_versions(self, pid: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT number, branch, message, author, parent, created_at FROM versions "
            "WHERE project_id=%s ORDER BY number DESC", (pid,)
        )
        return [_row(r) for r in rows]

    def get_version(self, pid: str, number: int, with_state: bool = True) -> dict[str, Any] | None:
        cols = "project_id, number, branch, message, author, parent, created_at"
        row = self._one(
            f"SELECT {cols}{', state_json' if with_state else ''} FROM versions WHERE project_id=%s AND number=%s",
            (pid, number),
        )
        if not row:
            return None
        out = _row(row)
        if with_state:
            out["state"] = _state(out.pop("state_json"))
        return out

    # ---- activity -------------------------------------------------------
    def log_activity(self, pid: str, branch: str, actor: str, summary: list[str], rev: int) -> None:
        if not summary:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO activity (project_id, branch, actor, summary, rev) VALUES (%s, %s, %s, %s, %s)",
                (pid, branch, actor, Jsonb(list(summary)), rev),
            )

    # ---- collaborative documents (binary Yjs state) -----------------------
    def get_ydoc(self, pid: str, branch: str) -> bytes | None:
        row = self._one("SELECT data FROM ydocs WHERE project_id=%s AND branch=%s", (pid, branch))
        return bytes(row["data"]) if row else None

    def put_ydoc(self, pid: str, branch: str, data: bytes) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO ydocs (project_id, branch, data) VALUES (%s, %s, %s) "
                "ON CONFLICT (project_id, branch) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
                (pid, branch, data),
            )

    def delete_ydoc(self, pid: str, branch: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM ydocs WHERE project_id=%s AND branch=%s", (pid, branch))

    # ---- OAuth clients, authorization codes and API tokens ----------------------
    # Secrets are never stored: codes and tokens are kept as SHA-256 hashes (see auth.py).
    def save_oauth_client(self, client_id: str, data: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute("INSERT INTO oauth_clients (client_id, data) VALUES (%s, %s) "
                         "ON CONFLICT (client_id) DO UPDATE SET data = EXCLUDED.data", (client_id, Jsonb(data)))

    def get_oauth_client(self, client_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT data FROM oauth_clients WHERE client_id=%s", (client_id,))
        return row["data"] if row else None

    def save_auth_code(self, code_hash: str, data: dict[str, Any], expires_at: float) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM oauth_codes WHERE expires_at < %s", (time.time(),))
            conn.execute("INSERT INTO oauth_codes (code_hash, data, expires_at) VALUES (%s, %s, %s) "
                         "ON CONFLICT (code_hash) DO UPDATE SET data = EXCLUDED.data, expires_at = EXCLUDED.expires_at",
                         (code_hash, Jsonb(data), expires_at))

    def get_auth_code(self, code_hash: str) -> dict[str, Any] | None:
        row = self._one("SELECT data FROM oauth_codes WHERE code_hash=%s AND expires_at >= %s", (code_hash, time.time()))
        return row["data"] if row else None

    def pop_auth_code(self, code_hash: str) -> dict[str, Any] | None:
        """Single use: atomically deletes and returns the code's data (None if missing or expired)."""
        row = self._one("DELETE FROM oauth_codes WHERE code_hash=%s RETURNING data, expires_at", (code_hash,))
        if not row or row["expires_at"] < time.time():
            return None
        return row["data"]

    def create_token(self, token_hash: str, kind: str, org_id: str, user_name: str, scopes: list[str],
                     client_id: str | None = None, name: str = "", expires_at: float | None = None,
                     resource: str | None = None) -> dict[str, Any]:
        tid = f"tok-{secrets.token_hex(6)}"
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO api_tokens (id, token_hash, kind, org_id, user_name, scopes, client_id, name, resource, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (tid, token_hash, kind, org_id, user_name, Jsonb(scopes), client_id, name, resource, expires_at))
        return self.get_token_by_id(tid)

    @staticmethod
    def _token(row) -> dict[str, Any] | None:
        if not row:
            return None
        d = _row(dict(row))
        d.pop("token_hash", None)
        return d

    def get_token(self, token_hash: str) -> dict[str, Any] | None:
        """Active token by hash (not revoked, not expired)."""
        return self._token(self._one(
            "SELECT * FROM api_tokens WHERE token_hash=%s AND NOT revoked AND (expires_at IS NULL OR expires_at >= %s)",
            (token_hash, time.time())))

    def get_token_by_id(self, token_id: str) -> dict[str, Any] | None:
        return self._token(self._one("SELECT * FROM api_tokens WHERE id=%s", (token_id,)))

    def touch_token(self, token_hash: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE api_tokens SET last_used_at = now() WHERE token_hash=%s", (token_hash,))

    def revoke_token(self, token_hash: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE api_tokens SET revoked = TRUE WHERE token_hash=%s", (token_hash,))

    def revoke_token_id(self, token_id: str) -> bool:
        row = self._one("UPDATE api_tokens SET revoked = TRUE WHERE id=%s AND NOT revoked RETURNING id", (token_id,))
        return row is not None

    def list_tokens(self, org_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM api_tokens WHERE org_id=%s AND NOT revoked"
        params: tuple = (org_id,)
        if kind:
            sql += " AND kind=%s"
            params += (kind,)
        return [self._token(r) for r in self._all(sql + " ORDER BY created_at DESC", params)]

    def list_activity(self, pid: str, branch: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT actor, summary, rev, created_at FROM activity WHERE project_id=%s AND branch=%s "
            "ORDER BY id DESC LIMIT %s", (pid, branch, limit)
        )
        return [_row(r) for r in rows]
