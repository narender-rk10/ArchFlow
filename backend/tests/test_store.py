"""Store contract tests, run against SQLite always and Postgres when TEST_DATABASE_URL is set.

    TEST_DATABASE_URL=postgresql://archflow:<POSTGRES_PASSWORD>@localhost:5432/archflow uv run pytest tests/test_store.py
"""

import os
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest

from archflow.models import ProjectState
from archflow.ops import apply_ops
from archflow.store import DEFAULT_ORG_ID, Store

PG_URL = os.environ.get("TEST_DATABASE_URL", "")
DEV_DB = Path(__file__).resolve().parent.parent / "data" / "archflow.db"


@pytest.fixture(scope="module")
def pg_store():
    if not PG_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    from archflow.store_pg import PostgresStore

    store = PostgresStore(PG_URL)
    yield store
    store.close()


@pytest.fixture(params=["sqlite", "postgres"])
def store(request):
    if request.param == "sqlite":
        s = Store(":memory:")
        yield s
        s.close()
    else:
        yield request.getfixturevalue("pg_store")


@pytest.fixture
def org(store):
    """A fresh org per test keeps Postgres runs isolated without truncating tables."""
    return store.create_org("Test Org")


def sample_state() -> ProjectState:
    """Deterministic sample (edge ids are random, so build once and hand out copies)."""
    return _SAMPLE.model_copy(deep=True)


def _build_sample() -> ProjectState:
    state, _ = apply_ops(ProjectState(), [
        {"op": "add_node", "node": {"name": "API", "type": "gateway"}},
        {"op": "add_node", "node": {"name": "DB", "type": "database", "technology": "PostgreSQL"}},
        {"op": "add_edge", "edge": {"source": "API", "target": "DB"}},
    ])
    return state


_SAMPLE = _build_sample()


# ---------------------------------------------------------------- orgs
def test_default_org_exists(store):
    default = store.get_org(DEFAULT_ORG_ID)
    assert default["name"] == "Default"
    assert DEFAULT_ORG_ID in {o["id"] for o in store.list_orgs()}


def test_orgs_and_members(store, org):
    assert org["id"].startswith("org-") and org["name"] == "Test Org" and org["created_at"]
    assert store.get_org(org["id"]) == org
    assert store.get_org("org-missing") is None
    assert org["id"] in {o["id"] for o in store.list_orgs()}

    tag = org["id"]  # unique per test run (Postgres is shared across runs)
    alice = store.create_user(f"alice+{tag}@example.com", "Alice", "scrypt$x")
    bob = store.create_user(f"bob+{tag}@example.com", "Bob", "scrypt$x")
    assert store.get_user_by_email(f"alice+{tag}@example.com")["id"] == alice["id"]
    assert "password_hash" not in alice and store.get_user_by_email(alice["email"], with_hash=True)["password_hash"]
    m = store.add_member(org["id"], alice["id"], "owner")
    assert m["org_id"] == org["id"] and m["user_id"] == alice["id"] and m["name"] == "Alice" and m["role"] == "owner"
    store.add_member(org["id"], bob["id"], "viewer")
    store.add_member(org["id"], bob["id"], "admin")  # upsert changes role
    members = {x["name"]: x["role"] for x in store.list_members(org["id"])}
    assert members == {"Alice": "owner", "Bob": "admin"}
    assert store.get_role(org["id"], bob["id"]) == "admin" and store.count_role(org["id"], "owner") == 1
    assert [o["role"] for o in store.list_user_orgs(alice["id"]) if o["id"] == org["id"]] == ["owner"]
    assert store.remove_member(org["id"], bob["id"]) and store.get_role(org["id"], bob["id"]) is None
    assert store.list_members(DEFAULT_ORG_ID) == [] or all(
        x["org_id"] == DEFAULT_ORG_ID for x in store.list_members(DEFAULT_ORG_ID))


def test_sessions_invites_and_ai_keys(store, org):
    import time as _t
    u = store.create_user(f"sess+{org['id']}@example.com", "S", "scrypt$x")
    store.create_session(f"h-{org['id']}", u["id"], _t.time() + 60, "pytest")
    assert store.get_session(f"h-{org['id']}")["user_id"] == u["id"]
    store.revoke_session(f"h-{org['id']}")
    assert store.get_session(f"h-{org['id']}") is None
    store.create_session(f"old-{org['id']}", u["id"], _t.time() - 1)
    assert store.get_session(f"old-{org['id']}") is None  # expired
    inv = store.create_invite(org["id"], f"ih-{org['id']}", "developer", _t.time() + 60, email=None, created_by=u["id"])
    assert store.get_invite(f"ih-{org['id']}")["id"] == inv["id"] and [i["id"] for i in store.list_invites(org["id"])] == [inv["id"]]
    assert store.accept_invite(inv["id"], u["id"]) and not store.accept_invite(inv["id"], u["id"])  # single use
    assert store.get_invite(f"ih-{org['id']}") is None
    store.set_ai_key(org["id"], "gemini", "v1:abc", "1234", u["id"])
    store.set_ai_key(org["id"], "gemini", "v1:def", "5678", u["id"])
    assert [(k["provider"], k["last4"]) for k in store.list_ai_keys(org["id"])] == [("gemini", "5678")]
    store.delete_ai_key(org["id"], "gemini")
    assert store.list_ai_keys(org["id"]) == []


# ---------------------------------------------------------------- projects
def test_project_crud_scoped_by_org(store, org):
    other = store.create_org("Other Org")
    p1 = store.create_project("One", "first", org_id=org["id"])
    p2 = store.create_project("Two", org_id=org["id"])
    p3 = store.create_project("Three", org_id=other["id"])
    assert set(p1) >= {"id", "name", "description", "created_at", "org_id"}
    assert p1["name"] == "One" and p1["description"] == "first" and p1["org_id"] == org["id"]

    assert {p["id"] for p in store.list_projects(org["id"])} == {p1["id"], p2["id"]}
    assert [p["id"] for p in store.list_projects(other["id"])] == [p3["id"]]
    all_ids = {p["id"] for p in store.list_projects()}
    assert {p1["id"], p2["id"], p3["id"]} <= all_ids

    assert store.get_project(p1["id"]) == p1
    store.delete_project(p1["id"])
    assert store.get_project(p1["id"]) is None
    assert store.get_state(p1["id"], "main") is None
    assert {p["id"] for p in store.list_projects(org["id"])} == {p2["id"]}


def test_create_project_defaults_to_default_org(store):
    p = store.create_project("Defaulted")
    try:
        assert p["org_id"] == DEFAULT_ORG_ID
        assert p["id"] in {x["id"] for x in store.list_projects(DEFAULT_ORG_ID)}
    finally:
        store.delete_project(p["id"])


# ---------------------------------------------------------------- branches / state
def test_state_save_load(store, org):
    p = store.create_project("S", state=sample_state(), org_id=org["id"])
    pid = p["id"]
    state = store.get_state(pid, "main")
    assert state == sample_state()
    assert store.get_state(pid, "nope") is None

    new, _ = apply_ops(state, [{"op": "add_node", "node": {"name": "Cache", "type": "cache"}}])
    store.save_state(pid, "main", new)
    loaded = store.get_state(pid, "main")
    assert loaded == new and "cache" in loaded.nodes and loaded.rev == new.rev


def test_versions_and_branches(store, org):
    pid = store.create_project("V", state=sample_state(), org_id=org["id"])["id"]
    assert store.head_version(pid, "main") is None
    assert store.list_versions(pid) == []

    s1 = store.get_state(pid, "main")
    v1 = store.commit(pid, "main", "first", "alice", s1)
    assert v1["number"] == 1 and v1["parent"] is None and v1["branch"] == "main"
    assert v1["author"] == "alice" and v1["message"] == "first" and "state" not in v1
    assert store.head_version(pid, "main") == 1

    s2, _ = apply_ops(s1, [{"op": "add_node", "node": {"name": "Queue", "type": "queue"}}])
    store.save_state(pid, "main", s2)
    v2 = store.commit(pid, "main", "second", "bob", s2)
    assert v2["number"] == 2 and v2["parent"] == 1

    full = store.get_version(pid, 1)
    assert full["state"] == s1 and full["project_id"] == pid
    assert store.get_version(pid, 99) is None
    listed = store.list_versions(pid)
    assert [v["number"] for v in listed] == [2, 1]
    assert set(listed[0]) == {"number", "branch", "message", "author", "parent", "created_at"}

    # branch from v1: first commit on the branch has parent = base_version
    store.create_branch(pid, "exp/x", full["state"], 1)
    assert "queue" not in store.get_state(pid, "exp/x").nodes
    branches = {b["name"]: b for b in store.list_branches(pid)}
    assert set(branches) == {"main", "exp/x"} and branches["exp/x"]["base_version"] == 1
    assert branches["main"]["base_version"] is None
    assert store.head_version(pid, "exp/x") is None
    v3 = store.commit(pid, "exp/x", "on branch", "carol", store.get_state(pid, "exp/x"))
    assert v3["number"] == 3 and v3["parent"] == 1 and v3["branch"] == "exp/x"
    assert store.head_version(pid, "main") == 2 and store.head_version(pid, "exp/x") == 3


def test_activity(store, org):
    pid = store.create_project("A", org_id=org["id"])["id"]
    store.log_activity(pid, "main", "alice", [], 1)  # empty summary is a no-op
    store.log_activity(pid, "main", "alice", ["added API"], 1)
    store.log_activity(pid, "main", "bob", ["added DB", "connected API → DB"], 2)
    store.log_activity(pid, "other", "carol", ["x"], 3)
    acts = store.list_activity(pid, "main")
    assert [a["actor"] for a in acts] == ["bob", "alice"]
    assert acts[0]["summary"] == ["added DB", "connected API → DB"] and acts[0]["rev"] == 2
    assert set(acts[0]) == {"actor", "summary", "rev", "created_at"}
    assert len(store.list_activity(pid, "main", limit=1)) == 1


def test_transaction_read_modify_write(store, org):
    pid = store.create_project("T", org_id=org["id"])["id"]
    with store.transaction(pid, "main") as state:
        new, summary = apply_ops(state, [{"op": "add_node", "node": {"name": "API"}}])
        store.save_state(pid, "main", new)
        store.log_activity(pid, "main", "alice", summary, new.rev)
    assert "api" in store.get_state(pid, "main").nodes
    assert store.list_activity(pid, "main")[0]["actor"] == "alice"
    with store.transaction(pid, "missing") as state:
        assert state is None


def test_code_graphs(store, org):
    pid = store.create_project("C", org_id=org["id"])["id"]
    assert store.latest_code_graph(pid) is None and store.list_code_graphs(pid) == []
    blob = b"\x78\x9c" + bytes(range(50))  # opaque bytes round-trip
    first = store.add_code_graph(pid, source="upload", repo_url=None, commit_sha=None, created_by="alice",
                                 summary={"stats": {"files": 1}}, graph=blob)
    second = store.add_code_graph(pid, source="git", repo_url="https://github.com/a/b", commit_sha="abc1234",
                                  created_by="bob", summary={"stats": {"files": 2}}, graph=b"zz")
    assert first["id"].startswith("cg-") and "graph" not in first and first["summary"] == {"stats": {"files": 1}}
    assert [g["id"] for g in store.list_code_graphs(pid)] == [second["id"], first["id"]]
    latest = store.latest_code_graph(pid, with_graph=True)
    assert latest["id"] == second["id"] and latest["graph"] == b"zz" and latest["commit_sha"] == "abc1234"
    assert store.get_code_graph(pid, first["id"], with_graph=True)["graph"] == blob
    assert store.get_code_graph("p-other", first["id"]) is None  # scoped by project
    store.delete_project(pid)
    assert store.list_code_graphs(pid) == []  # cascades


# ---------------------------------------------------------------- postgres specifics
def test_pg_transaction_rolls_back_on_error(pg_store):
    pid = pg_store.create_project("RB")["id"]
    try:
        with pytest.raises(RuntimeError):
            with pg_store.transaction(pid, "main") as state:
                pg_store.save_state(pid, "main", state.model_copy(update={"rev": 42}))
                raise RuntimeError("boom")
        assert pg_store.get_state(pid, "main").rev == 0
    finally:
        pg_store.delete_project(pid)


def test_pg_transaction_is_safe_across_processes(pg_store):
    """Two independent stores (separate pools and RLocks, like two server processes) racing on one
    branch: SELECT ... FOR UPDATE must serialize them so no increment is lost."""
    from archflow.store_pg import PostgresStore

    pid = pg_store.create_project("Race")["id"]
    stores = [PostgresStore(PG_URL, max_size=4), PostgresStore(PG_URL, max_size=4)]
    workers, rounds = 4, 15
    errors: list[BaseException] = []

    def work(s: PostgresStore) -> None:
        try:
            for _ in range(rounds):
                with s.transaction(pid, "main") as state:
                    s.save_state(pid, "main", state.model_copy(update={"rev": state.rev + 1}))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(stores[i % 2],)) for i in range(workers)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert not errors, errors
        assert pg_store.get_state(pid, "main").rev == workers * rounds
    finally:
        for s in stores:
            s.close()
        pg_store.delete_project(pid)


def test_pg_migrations_recorded_and_rls_scaffolded(pg_store):
    from archflow.store_pg import run_migrations

    with pg_store.pool.connection() as conn:
        assert run_migrations(conn) == []  # idempotent
        versions = [r["version"] for r in conn.execute("SELECT version FROM schema_migrations")]
        assert "001_init.sql" in versions
        rls = conn.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname='projects'").fetchone()
        assert rls == {"relrowsecurity": True, "relforcerowsecurity": False}
        assert conn.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone()


# ---------------------------------------------------------------- sqlite migration
OLD_SCHEMA = """
CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                       created_at TEXT NOT NULL);
CREATE TABLE branches (project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, name TEXT NOT NULL,
                       state_json TEXT NOT NULL, base_version INTEGER, created_at TEXT NOT NULL,
                       PRIMARY KEY (project_id, name));
"""


def test_sqlite_migrates_pre_org_db(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO projects VALUES ('p-old', 'Legacy', 'd', '2025-01-01T00:00:00+00:00')")
    conn.execute("INSERT INTO branches VALUES ('p-old', 'main', ?, NULL, '2025-01-01T00:00:00+00:00')",
                 (sample_state().model_dump_json(),))
    conn.commit()
    conn.close()

    store = Store(path)
    p = store.get_project("p-old")
    assert p["name"] == "Legacy" and p["org_id"] == DEFAULT_ORG_ID
    assert store.get_state("p-old", "main") == sample_state()
    new = store.create_project("New")
    assert {x["id"] for x in store.list_projects(DEFAULT_ORG_ID)} == {"p-old", new["id"]}
    store.close()
    Store(path).close()  # re-opening an already-migrated DB is a no-op


@pytest.mark.skipif(not DEV_DB.exists(), reason="no dev DB at backend/data/archflow.db")
def test_sqlite_migrates_copy_of_dev_db(tmp_path):
    """Migrate a COPY of the real dev DB; the original is never opened for writing."""
    copy = tmp_path / "archflow.db"
    shutil.copy2(DEV_DB, copy)
    raw = sqlite3.connect(copy)
    before = {r[0]: r for r in raw.execute("SELECT id, name, description, created_at FROM projects")}
    cols = {r[1] for r in raw.execute("PRAGMA table_info(projects)")}
    orgs_before = ({r[0]: r[1] for r in raw.execute("SELECT id, org_id FROM projects")}
                   if "org_id" in cols else {pid: DEFAULT_ORG_ID for pid in before})
    counts = {t: raw.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("projects", "branches", "versions", "activity")}
    raw.close()

    store = Store(copy)
    after = store.list_projects()
    assert {p["id"]: (p["id"], p["name"], p["description"], p["created_at"]) for p in after} == before
    # Pre-org databases move everything to the default org; already-migrated ones keep their orgs.
    assert {p["id"]: p["org_id"] for p in after} == orgs_before
    for pid in before:
        assert store.get_state(pid, "main") is not None
    raw = sqlite3.connect(copy)
    for t, n in counts.items():
        assert raw.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == n
    raw.close()
    store.close()
