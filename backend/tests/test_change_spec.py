"""AI-written OpenSpec changes (vibe coding). No network: the LLM is faked."""

import pytest
from archflow import change_spec as cs
from archflow import llm
from archflow.models import ProjectState
from archflow.templates import build_template


@pytest.fixture()
def no_llm(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda org_id=None: False)


def test_offline_change_covers_scope(no_llm):
    state = build_template("ai-customer-support")
    spec = cs.generate(state, "Support", ["order-service"], [])
    assert spec.engine == "offline" and cs.ID_RE.match(spec.change_id)
    assert {"proposal.md", "design.md", "tasks.md"} <= set(spec.files)
    specs = "".join(v for k, v in spec.files.items() if k.startswith("specs/"))
    assert specs.startswith("## Purpose\nDefines the core behaviour of the system")
    assert "## ADDED Requirements" in specs and "SHALL" in specs and "- **WHEN**" in specs
    assert "## Capabilities" in spec.files["proposal.md"] and "- `core-behaviour`:" in spec.files["proposal.md"]
    for rid in state.nodes["order-service"].requirement_ids:
        assert f"_Source: {rid}" in specs and f"Test {rid}" in spec.files["tasks.md"]
    assert "- [ ] 1.1 " in spec.files["tasks.md"]
    assert "Orders DB" in spec.files["design.md"]  # neighbours named in the architecture section
    assert spec.prompt.startswith(f"# Implement OpenSpec change `{spec.change_id}`")
    assert f"## File: openspec/changes/{spec.change_id}/tasks.md" in spec.prompt


def test_whole_system_and_empty(no_llm):
    state = build_template("ai-customer-support")
    spec = cs.generate(state, "Support", [], [])
    assert all(f"Test {rid}" in spec.files["tasks.md"] for rid in state.requirements)
    with pytest.raises(ValueError, match="nothing to specify"):
        cs.generate(ProjectState(), "Empty", [], [])


def test_llm_output_is_normalized_and_completed(monkeypatch):
    state = build_template("ai-customer-support")
    seen = {}

    def fake(system, messages, schema, **kw):
        seen.update(context=messages[0]["content"], **kw)
        return {"change_id": "Add Order Lookup!!", "title": "Add order lookup",
                "proposal": {"why": "Agents need order status.", "what_changes": ["Add lookup endpoint"]},
                "specs": [{"capability": "Order Lookup", "requirements": [
                    {"name": "Lookup", "statement": "return the latest order", "sources": ["REQ-003", "REQ-999"],
                     "scenarios": [{"name": "found", "when": "a customer asks", "then": "the order is returned"}]}]}],
                "design": {"context": "FastAPI service."},
                "tasks": [{"section": "api", "items": ["Add GET /orders/latest", "  "]}]}

    monkeypatch.setattr(llm, "available", lambda org_id=None: True)
    monkeypatch.setattr(llm, "complete_json", fake)
    spec = cs.generate(state, "Support", ["order-service"], [], memory="<project_memory>\n- AWS only\n</project_memory>",
                       decisions=[{"id": "ADR-001", "title": "Use Postgres", "status": "accepted", "decision": "Postgres"}],
                       extra="Use pytest", target="codex", org_id="org-1")
    assert spec.engine == "llm" and spec.change_id == "add-order-lookup"
    assert seen["group"] == "reasoning" and seen["org_id"] == "org-1"
    for needle in ('"name": "Order Service"', '"name": "Orders DB"', "AWS only", "ADR-001", "Use pytest", "OpenAI Codex CLI"):
        assert needle in seen["context"], needle
    spec_file = spec.files["specs/order-lookup/spec.md"]
    assert "The system SHALL return the latest order" in spec_file  # SHALL enforced
    assert "REQ-999" not in spec_file  # unknown ids dropped
    other = [r for r in state.nodes["order-service"].requirement_ids if r != "REQ-003"]
    for rid in other:  # not covered by the model: derived and noted
        assert any(rid in n for n in spec.notes) and f"_Source: {rid}" in "".join(spec.files.values())
        assert f"Test {rid}" in spec.files["tasks.md"]
    assert "## 1. Api" in spec.files["tasks.md"] and "1.2 " not in spec.files["tasks.md"].split("## 2.")[0]


def test_llm_failure_falls_back(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda org_id=None: True)

    def down(*a, **k):
        raise llm.LLMUnavailable("all providers failed")

    monkeypatch.setattr(llm, "complete_json", down)
    spec = cs.generate(build_template("ai-customer-support"), "Support", ["order-service"], [])
    assert spec.engine == "offline" and "could not write" in spec.notes[0]
