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
from .store import (CODE_GRAPH_COLS, CONVERSATION_COLS, CONVERSATION_FIELDS, DECISION_COLS, DECISION_JSON_FIELDS,
                    DEFAULT_ORG_ID, DEFAULT_ORG_NAME, MEMORY_COLS, MESSAGE_COLS, check_decision_fields,
                    check_memory_fields, code_graph_id, conversation_id, decision_id, memory_id)

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

    def update_org(self, org_id: str, name: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            conn.execute("UPDATE orgs SET name=%s WHERE id=%s", (name, org_id))
        return self.get_org(org_id)

    # ---- memberships ------------------------------------------------------
    _MEMBER_SQL = ("SELECT m.org_id, m.user_id, u.email, u.name, m.role, m.created_at FROM memberships m "
                   "JOIN users u ON u.id = m.user_id")

    def add_member(self, org_id: str, user_id: str, role: str) -> dict[str, Any]:
        """Add a member or change their role (upsert)."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO memberships (org_id, user_id, role) VALUES (%s, %s, %s) "
                "ON CONFLICT (org_id, user_id) DO UPDATE SET role = EXCLUDED.role", (org_id, user_id, role))
        return self.get_member(org_id, user_id)

    def get_member(self, org_id: str, user_id: str) -> dict[str, Any] | None:
        return _row(self._one(f"{self._MEMBER_SQL} WHERE m.org_id=%s AND m.user_id=%s", (org_id, user_id)))

    def get_role(self, org_id: str, user_id: str) -> str | None:
        row = self._one("SELECT role FROM memberships WHERE org_id=%s AND user_id=%s", (org_id, user_id))
        return row["role"] if row else None

    def list_members(self, org_id: str) -> list[dict[str, Any]]:
        rows = self._all(f"{self._MEMBER_SQL} WHERE m.org_id=%s ORDER BY m.created_at, u.email", (org_id,))
        return [_row(r) for r in rows]

    def remove_member(self, org_id: str, user_id: str) -> bool:
        row = self._one("DELETE FROM memberships WHERE org_id=%s AND user_id=%s RETURNING user_id", (org_id, user_id))
        return row is not None

    def count_role(self, org_id: str, role: str) -> int:
        return self._one("SELECT COUNT(*) AS n FROM memberships WHERE org_id=%s AND role=%s", (org_id, role))["n"]

    def list_user_orgs(self, user_id: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT o.id, o.name, o.created_at, m.role FROM memberships m JOIN orgs o ON o.id = m.org_id "
            "WHERE m.user_id=%s ORDER BY o.created_at, o.id", (user_id,))
        return [_row(r) for r in rows]

    # ---- users and sessions -------------------------------------------------
    _USER_COLS = "id, email, name, created_at, last_login_at"

    def count_users(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM users")["n"]

    def create_user(self, email: str, name: str, password_hash: str) -> dict[str, Any]:
        uid = f"u-{secrets.token_hex(6)}"
        with self._conn() as conn:
            conn.execute("INSERT INTO users (id, email, name, password_hash) VALUES (%s, %s, %s, %s)",
                         (uid, email, name, password_hash))
        return self.get_user(uid)

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        return _row(self._one(f"SELECT {self._USER_COLS} FROM users WHERE id=%s", (user_id,)))

    def get_user_by_email(self, email: str, with_hash: bool = False) -> dict[str, Any] | None:
        cols = self._USER_COLS + (", password_hash" if with_hash else "")
        return _row(self._one(f"SELECT {cols} FROM users WHERE email=%s", (email,)))

    def touch_login(self, user_id: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE users SET last_login_at = now() WHERE id=%s", (user_id,))

    def create_session(self, token_hash: str, user_id: str, expires_at: float, user_agent: str = "") -> None:
        with self._conn() as conn:
            conn.execute("INSERT INTO sessions (token_hash, user_id, expires_at, user_agent) VALUES (%s, %s, %s, %s)",
                         (token_hash, user_id, expires_at, user_agent[:300]))

    def get_session(self, token_hash: str) -> dict[str, Any] | None:
        """Active session (not revoked, not expired)."""
        return _row(self._one(
            "SELECT token_hash, user_id, created_at, expires_at, user_agent FROM sessions "
            "WHERE token_hash=%s AND NOT revoked AND expires_at >= %s", (token_hash, time.time())))

    def extend_session(self, token_hash: str, expires_at: float) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE sessions SET expires_at=%s WHERE token_hash=%s", (expires_at, token_hash))

    def revoke_session(self, token_hash: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE sessions SET revoked = TRUE WHERE token_hash=%s", (token_hash,))

    # ---- invites --------------------------------------------------------------
    _INVITE_COLS = "id, org_id, email, role, created_by, created_at, expires_at, accepted_at, accepted_by, revoked"

    def create_invite(self, org_id: str, token_hash: str, role: str, expires_at: float, email: str | None = None,
                      created_by: str | None = None) -> dict[str, Any]:
        iid = f"inv-{secrets.token_hex(6)}"
        return _row(self._one(
            "INSERT INTO invites (id, org_id, token_hash, email, role, created_by, expires_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {self._INVITE_COLS}",
            (iid, org_id, token_hash, email, role, created_by, expires_at)))

    def get_invite(self, token_hash: str) -> dict[str, Any] | None:
        """Usable invite by hash (not revoked, not accepted, not expired)."""
        return _row(self._one(
            f"SELECT {self._INVITE_COLS} FROM invites WHERE token_hash=%s AND NOT revoked AND accepted_at IS NULL "
            "AND expires_at >= %s", (token_hash, time.time())))

    def list_invites(self, org_id: str) -> list[dict[str, Any]]:
        rows = self._all(
            f"SELECT {self._INVITE_COLS} FROM invites WHERE org_id=%s AND NOT revoked AND accepted_at IS NULL "
            "AND expires_at >= %s ORDER BY created_at DESC", (org_id, time.time()))
        return [_row(r) for r in rows]

    def revoke_invite(self, org_id: str, invite_id: str) -> bool:
        row = self._one("UPDATE invites SET revoked = TRUE WHERE id=%s AND org_id=%s AND NOT revoked "
                        "AND accepted_at IS NULL RETURNING id", (invite_id, org_id))
        return row is not None

    def accept_invite(self, invite_id: str, user_id: str) -> bool:
        """Mark an invite used (single use, atomic). False if it was already used, revoked or expired."""
        row = self._one("UPDATE invites SET accepted_at = now(), accepted_by=%s WHERE id=%s AND accepted_at IS NULL "
                        "AND NOT revoked AND expires_at >= %s RETURNING id", (user_id, invite_id, time.time()))
        return row is not None

    # ---- per-org AI provider keys (encrypted by the caller, see secrets_box.py) ----------
    def set_ai_key(self, org_id: str, provider: str, ciphertext: str, last4: str, user_id: str | None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO org_ai_keys (org_id, provider, ciphertext, last4, updated_by) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (org_id, provider) DO UPDATE SET ciphertext = EXCLUDED.ciphertext, last4 = EXCLUDED.last4, "
                "updated_by = EXCLUDED.updated_by, updated_at = now()", (org_id, provider, ciphertext, last4, user_id))

    def delete_ai_key(self, org_id: str, provider: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM org_ai_keys WHERE org_id=%s AND provider=%s", (org_id, provider))

    def list_ai_keys(self, org_id: str) -> list[dict[str, Any]]:
        rows = self._all("SELECT provider, ciphertext, last4, updated_by, updated_at FROM org_ai_keys "
                         "WHERE org_id=%s ORDER BY provider", (org_id,))
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
                     resource: str | None = None, user_id: str | None = None) -> dict[str, Any]:
        tid = f"tok-{secrets.token_hex(6)}"
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO api_tokens (id, token_hash, kind, org_id, user_name, user_id, scopes, client_id, name, "
                "resource, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (tid, token_hash, kind, org_id, user_name, user_id, Jsonb(scopes), client_id, name, resource, expires_at))
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

    def revoke_user_tokens(self, org_id: str, user_id: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE api_tokens SET revoked = TRUE WHERE org_id=%s AND user_id=%s", (org_id, user_id))

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

    # ---- architecture decision records ----------------------------------
    def create_decision(self, pid: str, title: str, *, author: str = "anonymous", **fields: Any) -> dict[str, Any]:
        data = check_decision_fields({"title": title, **fields})
        data.setdefault("status", "proposed")
        with self.lock, self._conn() as conn, conn.transaction():
            # Serialize ADR numbering per project across processes.
            conn.execute("SELECT 1 FROM projects WHERE id=%s FOR UPDATE", (pid,))
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM decisions WHERE project_id=%s", (pid,)).fetchone()["n"]
            did = decision_id(seq)
            conn.execute(
                "INSERT INTO decisions (project_id, id, seq, title, status, context, decision, consequences, "
                "alternatives, chosen, requirement_ids, node_ids, evaluation, author) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (pid, did, seq, data["title"].strip(), data["status"], data.get("context", ""),
                 data.get("decision", ""), data.get("consequences", ""), Jsonb(data.get("alternatives", [])),
                 data.get("chosen", ""), Jsonb(data.get("requirement_ids", [])), Jsonb(data.get("node_ids", [])),
                 Jsonb(data["evaluation"]) if data.get("evaluation") is not None else None, author),
            )
        return self.get_decision(pid, did)

    def list_decisions(self, pid: str) -> list[dict[str, Any]]:
        rows = self._all(f"SELECT {DECISION_COLS} FROM decisions WHERE project_id=%s ORDER BY seq", (pid,))
        return [_row(r) for r in rows]

    def get_decision(self, pid: str, did: str) -> dict[str, Any] | None:
        return _row(self._one(f"SELECT {DECISION_COLS} FROM decisions WHERE project_id=%s AND id=%s", (pid, did)))

    def update_decision(self, pid: str, did: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        """Update the given fields (see DECISION_FIELDS); returns the decision, or None if it does not exist."""
        data = check_decision_fields(fields)
        if data:
            sets, params = [], []
            for k, v in data.items():
                sets.append(f"{k}=%s")
                params.append(Jsonb(v) if k in DECISION_JSON_FIELDS and v is not None else v)
            with self._conn() as conn:
                conn.execute(
                    f"UPDATE decisions SET {', '.join(sets)}, updated_at = now() WHERE project_id=%s AND id=%s",
                    (*params, pid, did))
        return self.get_decision(pid, did)

    # ---- code graphs (repository analysis) ------------------------------------
    @staticmethod
    def _code_graph(row: dict[str, Any] | None, with_graph: bool = False) -> dict[str, Any] | None:
        d = _row(row)
        if d is None:
            return None
        d["summary"] = d.get("summary") or {}
        if with_graph:
            d["graph"] = bytes(d["graph"]) if d.get("graph") is not None else None
        else:
            d.pop("graph", None)
        return d

    def add_code_graph(self, pid: str, *, source: str, repo_url: str | None, commit_sha: str | None,
                       created_by: str, status: str = "ready", summary: dict[str, Any] | None = None,
                       graph: bytes | None = None) -> dict[str, Any]:
        gid = code_graph_id()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO code_graphs (id, project_id, source, repo_url, commit_sha, created_by, status, summary, "
                "graph) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (gid, pid, source, repo_url, commit_sha, created_by, status, Jsonb(summary or {}), graph))
        return self.get_code_graph(pid, gid)

    def get_code_graph(self, pid: str, gid: str, with_graph: bool = False) -> dict[str, Any] | None:
        cols = CODE_GRAPH_COLS + (", graph" if with_graph else "")
        return self._code_graph(self._one(f"SELECT {cols} FROM code_graphs WHERE project_id=%s AND id=%s", (pid, gid)),
                                with_graph)

    def list_code_graphs(self, pid: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._all(f"SELECT {CODE_GRAPH_COLS} FROM code_graphs WHERE project_id=%s "
                         "ORDER BY created_at DESC, id DESC LIMIT %s", (pid, limit))
        return [self._code_graph(r) for r in rows]

    def latest_code_graph(self, pid: str, with_graph: bool = False) -> dict[str, Any] | None:
        cols = CODE_GRAPH_COLS + (", graph" if with_graph else "")
        return self._code_graph(self._one(f"SELECT {cols} FROM code_graphs WHERE project_id=%s AND status='ready' "
                                          "ORDER BY created_at DESC, id DESC LIMIT 1", (pid,)), with_graph)

    # ---- conversations (AI chat threads) -----------------------------------
    _CONV_SELECT = (f"SELECT {CONVERSATION_COLS}, (SELECT COUNT(*) FROM conversation_messages m "
                    "WHERE m.conversation_id = c.id) AS message_count FROM conversations c")

    def create_conversation(self, pid: str, branch: str, kind: str, title: str, created_by: str,
                            created_by_id: str | None = None) -> dict[str, Any]:
        cid = conversation_id()
        with self._conn() as conn:
            conn.execute("INSERT INTO conversations (id, project_id, branch, kind, title, created_by, created_by_id) "
                         "VALUES (%s, %s, %s, %s, %s, %s, %s)", (cid, pid, branch, kind, title, created_by, created_by_id))
        return self.get_conversation(cid)

    def get_conversation(self, cid: str) -> dict[str, Any] | None:
        return _row(self._one(f"{self._CONV_SELECT} WHERE id=%s", (cid,)))

    def list_conversations(self, pid: str, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        where, params = "project_id=%s", [pid]
        if kind:
            where += " AND kind=%s"
            params.append(kind)
        rows = self._all(f"{self._CONV_SELECT} WHERE {where} ORDER BY updated_at DESC, id DESC LIMIT %s",
                         (*params, limit))
        return [_row(r) for r in rows]

    def update_conversation(self, cid: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        data = {k: v for k, v in fields.items() if k in CONVERSATION_FIELDS}
        if data:
            sets = ", ".join(f"{k}=%s" for k in data)
            with self._conn() as conn:
                conn.execute(f"UPDATE conversations SET {sets}, updated_at = now() WHERE id=%s", (*data.values(), cid))
        return self.get_conversation(cid)

    def delete_conversation(self, cid: str) -> bool:
        with self._conn() as conn:
            return conn.execute("DELETE FROM conversations WHERE id=%s", (cid,)).rowcount > 0

    def add_message(self, cid: str, role: str, mode: str, content: str, data: dict[str, Any] | None,
                    author: str) -> dict[str, Any]:
        with self._conn() as conn, conn.transaction():
            # Serialize message numbering per conversation across processes.
            conn.execute("SELECT 1 FROM conversations WHERE id=%s FOR UPDATE", (cid,))
            seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM conversation_messages "
                               "WHERE conversation_id=%s", (cid,)).fetchone()["n"]
            row = conn.execute(
                f"INSERT INTO conversation_messages (conversation_id, seq, role, mode, content, data, author) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {MESSAGE_COLS}",
                (cid, seq, role, mode, content, Jsonb(data or {}), author)).fetchone()
            conn.execute("UPDATE conversations SET updated_at = now() WHERE id=%s", (cid,))
        return _row(row)

    def list_messages(self, cid: str, after_seq: int = 0) -> list[dict[str, Any]]:
        rows = self._all(f"SELECT {MESSAGE_COLS} FROM conversation_messages WHERE conversation_id=%s AND seq > %s "
                         "ORDER BY seq", (cid, after_seq))
        return [_row(r) for r in rows]

    def delete_messages_from(self, cid: str, seq: int) -> int:
        """Delete messages with seq >= ``seq`` (regenerate, clear); the summary is reset if it covered them."""
        with self._conn() as conn, conn.transaction():
            n = conn.execute("DELETE FROM conversation_messages WHERE conversation_id=%s AND seq >= %s",
                             (cid, seq)).rowcount
            conn.execute("UPDATE conversations SET summary = CASE WHEN summarized_seq >= %s THEN '' ELSE summary END, "
                         "summarized_seq = CASE WHEN summarized_seq >= %s THEN 0 ELSE summarized_seq END, "
                         "updated_at = now() WHERE id=%s", (seq, seq, cid))
        return n

    # ---- project memory (facts every AI call sees) -----------------------------
    def list_memory(self, pid: str) -> list[dict[str, Any]]:
        rows = self._all(f"SELECT {MEMORY_COLS} FROM project_memory WHERE project_id=%s "
                         "ORDER BY pinned DESC, created_at DESC, id DESC", (pid,))
        return [_row(r) for r in rows]

    def get_memory(self, pid: str, mid: str) -> dict[str, Any] | None:
        return _row(self._one(f"SELECT {MEMORY_COLS} FROM project_memory WHERE project_id=%s AND id=%s", (pid, mid)))

    def add_memory(self, pid: str, text: str, *, category: str = "fact", pinned: bool = False, source: str = "user",
                   conversation_id: str | None = None, created_by: str = "anonymous") -> dict[str, Any]:
        data = check_memory_fields({"text": text, "category": category, "pinned": pinned})
        mid = memory_id()
        with self._conn() as conn:
            conn.execute("INSERT INTO project_memory (id, project_id, text, category, pinned, source, conversation_id, "
                         "created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                         (mid, pid, data["text"], data["category"], data["pinned"], source, conversation_id, created_by))
        return self.get_memory(pid, mid)

    def update_memory(self, pid: str, mid: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        data = check_memory_fields(fields)
        if data:
            sets = ", ".join(f"{k}=%s" for k in data)
            with self._conn() as conn:
                conn.execute(f"UPDATE project_memory SET {sets}, updated_at = now() WHERE project_id=%s AND id=%s",
                             (*data.values(), pid, mid))
        return self.get_memory(pid, mid)

    def delete_memory(self, pid: str, mid: str) -> bool:
        with self._conn() as conn:
            return conn.execute("DELETE FROM project_memory WHERE project_id=%s AND id=%s", (pid, mid)).rowcount > 0
