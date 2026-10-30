"""OpenSpec in ArchFlow: changes written through the real OpenSpec CLI, stored in the DB (LLM faked).

Pipeline tests need Node.js 20.19+ and `npm ci --omit=dev` in backend/openspec."""

import io
import time
import zipfile

import pytest
from authhelp import member_client, new_client, signup

from archflow import llm
from archflow import openspec_service as osvc
from archflow.main import create_app
from archflow.store import Store
from archflow.templates import build_template

needs_openspec = pytest.mark.skipif(not osvc.available(), reason="OpenSpec CLI not installed (backend/openspec)")


@pytest.fixture()
def no_llm(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda org_id=None: False)


@pytest.fixture()
def project():
    store = Store(":memory:")
    pid = store.create_project("Support", state=build_template("ai-customer-support"))["id"]
    return store, pid


def test_safe_path():
    assert osvc.safe_path("openspec/specs/a/spec.md") == "openspec/specs/a/spec.md"
    for bad in ("../etc/passwd", "openspec/../x", "specs/a.md", "openspec//x", "/openspec/x", "openspec/a b.md"):
        with pytest.raises(ValueError):
            osvc.safe_path(bad)


def test_project_config_carries_memory():
    cfg = osvc.project_config("<project_memory>\n- [constraint] AWS only\n</project_memory>", "Use pytest")
    assert cfg.startswith("schema: spec-driven") and "  - [constraint] AWS only" in cfg and "Team instructions: Use pytest" in cfg
    assert "rules:\n  tasks:" in cfg


def test_without_openspec_the_derived_change_is_stored(project, no_llm, monkeypatch):
    store, pid = project
    monkeypatch.setattr(osvc, "available", lambda: False)
    c = osvc.generate(store, pid, store.get_state(pid, "main"), "Support", ["order-service"], [], actor="Owner")
    assert (c["engine"], c["status"]) == ("archflow", "draft") and "not installed" in c["validation"]["skipped"]
    assert {"proposal.md", "design.md", "tasks.md"} <= set(osvc.change_files(store, pid, c))


@needs_openspec
def test_offline_change_validates_and_archives(project, no_llm):
    store, pid = project
    steps = []
    c = osvc.generate(store, pid, store.get_state(pid, "main"), "Support", ["order-service"], [], actor="Owner",
                      progress=steps.append)
    assert c["engine"] == "openspec" and c["status"] == "valid", c["validation"]
    assert steps[-1] == "Validating with OpenSpec"
    paths = {f["path"] for f in store.list_openspec_files(pid)}
    assert "openspec/config.yaml" in paths and f"openspec/changes/{c['change_id']}/.openspec.yaml" in paths
    again = osvc.generate(store, pid, store.get_state(pid, "main"), "Support", ["order-service"], [], actor="Owner")
    assert again["change_id"] == c["change_id"] + "-2"  # ids stay unique
    done = osvc.archive(store, pid, c["change_id"], "Owner")
    assert done["status"] == "archived" and done["archived_as"].endswith(c["change_id"])
    spec = store.get_openspec_file(pid, "openspec/specs/core-behaviour/spec.md")
    assert spec and "### Requirement:" in spec["content"]  # merged into the living specs
    assert osvc.change_files(store, pid, done)  # still readable from the archive


@needs_openspec
def test_llm_change_follows_openspec_workflow_and_repairs(project, monkeypatch):
    store, pid = project
    seen = {"artifacts": [], "repair": 0}
    bad_spec = "## ADDED Requirements\n\n### Requirement: Lookup\nThe system SHALL return the latest order.\n"
    good_spec = ("## Purpose\nLets customers and the support agent look up their latest order and its status.\n\n"
                 + bad_spec + "\n#### Scenario: Found\n- **WHEN** a customer asks\n- **THEN** the order is returned\n")

    def complete_json(system, messages, schema, **kw):
        if "Name an OpenSpec change" in system:
            return {"change_id": "add-order-lookup", "title": "Add order lookup"}
        if "fix an OpenSpec change" in system:
            seen["repair"] += 1
            assert "must include at least one scenario" in messages[0]["content"]
            return {"files": [{"path": "specs/order-lookup/spec.md", "content": good_spec}]}
        seen["artifacts"].append("specs")
        assert "Create specification files" in system  # OpenSpec's own instruction for the specs artifact
        assert "AWS only" in system  # project context from openspec/config.yaml
        assert "## Capabilities" in messages[0]["content"]  # the proposal it depends on
        return {"files": [{"path": "specs/order-lookup/spec.md", "content": bad_spec}]}

    def complete_tools(system, messages, tools, **kw):
        if "establishes WHY this change is needed" in system:
            seen["artifacts"].append("proposal")
            return ("## Why\nCustomers ask where their order is.\n\n## What Changes\n- Add order lookup\n\n"
                    "## Capabilities\n### New Capabilities\n- `order-lookup`: look up orders\n\n## Impact\n- Order Service\n"), []
        if "task list" in system:
            seen["artifacts"].append("tasks")
            assert "Name a test for every requirement" in system or "Include a test task" in system  # config rules
            return "## 1. API\n- [ ] 1.1 Add the endpoint\n- [ ] 1.2 Test REQ-003\n", []
        seen["artifacts"].append("design")
        return "```markdown\n## Context\nFastAPI service.\n\n## Decisions\n- Use Postgres\n```", []

    monkeypatch.setattr(llm, "available", lambda org_id=None: True)
    monkeypatch.setattr(llm, "complete_json", complete_json)
    monkeypatch.setattr(llm, "complete_tools", complete_tools)
    c = osvc.generate(store, pid, store.get_state(pid, "main"), "Support", ["order-service"], [], actor="Owner",
                      memory="<project_memory>\n- [constraint] AWS only\n</project_memory>")
    assert seen["artifacts"][0] == "proposal" and seen["artifacts"][-1] == "tasks"
    assert set(seen["artifacts"]) == {"proposal", "specs", "design", "tasks"}
    assert c["change_id"] == "add-order-lookup" and c["engine"] == "openspec+llm"
    assert seen["repair"] == 1 and c["status"] == "valid" and c["validation"]["repairs"] == 1
    files = osvc.change_files(store, pid, c)
    assert files["design.md"].startswith("## Context")  # code fence stripped
    assert "#### Scenario: Found" in files["specs/order-lookup/spec.md"]


# --------------------------------------------------------------------------- REST
@pytest.fixture()
def client():
    app = create_app(Store(":memory:"))
    with new_client(app) as c:
        signup(c)
        yield c


def wait_job(client, pid, job_id, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/projects/{pid}/openspec/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.3)
    raise AssertionError("job did not finish")


@needs_openspec
def test_openspec_rest_lifecycle(client, no_llm):
    pid = client.post("/api/projects", json={"name": "Shop", "template": "ai-customer-support"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/openspec/changes", json={"requirement_ids": ["REQ-003"], "use_ai": False})
    assert r.status_code == 202, r.text
    job = wait_job(client, pid, r.json()["id"])
    assert job["status"] == "done", job
    cid = job["change_id"]
    over = client.get(f"/api/projects/{pid}/openspec").json()
    assert over["available"] and [c["change_id"] for c in over["changes"]] == [cid]
    got = client.get(f"/api/projects/{pid}/openspec/changes/{cid}", params={"target": "codex"}).json()
    assert got["change"]["status"] == "valid" and "tasks.md" in got["files"] and "OpenAI Codex CLI" in got["prompt"]

    # Break a spec: OpenSpec reports it; fix it again.
    spec_path = next(k for k in got["files"] if k.startswith("specs/"))
    broken = client.put(f"/api/projects/{pid}/openspec/changes/{cid}/files",
                        json={"path": spec_path, "content": "## ADDED Requirements\n\n### Requirement: X\nThe system SHALL x.\n"})
    assert broken.json()["status"] == "invalid" and broken.json()["validation"]["issues"]
    fixed = client.put(f"/api/projects/{pid}/openspec/changes/{cid}/files",
                       json={"path": spec_path, "content": got["files"][spec_path]})
    assert fixed.json()["status"] == "valid"
    assert client.put(f"/api/projects/{pid}/openspec/changes/{cid}/files",
                      json={"path": "../../x.md", "content": "x"}).status_code == 422
    assert client.put(f"/api/projects/{pid}/openspec/changes/{cid}/files",
                      json={"path": ".openspec.yaml", "content": "x"}).status_code == 422

    viewer = member_client(client.app, client, "viewer", "viewer@example.com")
    assert viewer.get(f"/api/projects/{pid}/openspec/changes/{cid}").status_code == 200
    assert viewer.post(f"/api/projects/{pid}/openspec/changes/{cid}/archive").status_code == 403
    assert viewer.post(f"/api/projects/{pid}/openspec/changes", json={}).status_code == 403

    archived = client.post(f"/api/projects/{pid}/openspec/changes/{cid}/archive")
    assert archived.status_code == 200 and archived.json()["status"] == "archived"
    assert client.delete(f"/api/projects/{pid}/openspec/changes/{cid}").status_code == 409
    specs = client.get(f"/api/projects/{pid}/openspec").json()["specs"]
    assert specs and specs[0]["path"].startswith("openspec/specs/")
    living = client.get(f"/api/projects/{pid}/openspec/file", params={"path": specs[0]["path"]}).json()
    assert "### Requirement:" in living["content"]
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/projects/{pid}/openspec.zip").content))
    assert specs[0]["path"] in z.namelist() and any("/changes/archive/" in n for n in z.namelist())
    assert {"CLAUDE.md", "AGENTS.md", ".cursorrules"} <= set(z.namelist())  # coding-agent guides at the root


def test_openspec_job_errors(client, no_llm):
    empty = client.post("/api/projects", json={"name": "Empty", "template": "blank"}).json()["id"]
    r = client.post(f"/api/projects/{empty}/openspec/changes", json={})
    job = wait_job(client, empty, r.json()["id"])
    assert job["status"] == "failed" and "nothing to specify" in job["error"]
    assert client.get(f"/api/projects/{empty}/openspec/jobs/nope").status_code == 404
    assert client.get(f"/api/projects/{empty}/openspec/changes/nope").status_code == 404
