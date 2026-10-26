"""Multi-agent architecture review (specialists in parallel) and clarifying questions with answer options."""

import os
import threading

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import pytest  # noqa: E402
from authhelp import new_client, signup  # noqa: E402

from archflow import ai, llm, review  # noqa: E402
from archflow.agents import tracing  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.store import Store  # noqa: E402
from archflow.templates import build_template  # noqa: E402

from test_mcp_remote import BASE  # noqa: E402


def specialist_llm(fail: str | None = None, barrier: threading.Barrier | None = None):
    def complete_json(system, messages, schema, **kw):
        focus = next(k for k, v in review.SPECIALISTS.items() if v["label"] in system)
        if barrier:
            barrier.wait(timeout=10)  # every specialist must be running at the same time
        if focus == fail:
            raise llm.LLMUnavailable("tier down")
        return {"summary": f"{focus} ok", "findings": [
            {"severity": "high" if focus == "reliability" else "low", "title": f"{focus}: Orders DB is a single point",
             "detail": "d", "components": ["Orders DB", "Nonexistent"], "requirement_ids": ["REQ-003", "REQ-999"],
             "fix": "Add a replica for Orders DB"},
            {"severity": "bogus", "title": "  ", "detail": "", "components": [], "fix": ""}]}
    return complete_json


def test_specialists_run_in_parallel_and_merge(monkeypatch):
    monkeypatch.setattr(llm, "complete_json", specialist_llm(barrier=threading.Barrier(len(review.SPECIALISTS))))
    state = build_template("ai-customer-support")
    with tracing.listen(lambda t: None) as traces:
        out = review.review(state)
    assert [f["focus"] for f in out["findings"]][0] == "reliability"  # most severe first
    assert len(out["findings"]) == 4 and all(f["components"] == ["Orders DB"] for f in out["findings"])
    assert all(f["requirement_ids"] == ["REQ-003"] for f in out["findings"])
    assert {v["status"] for v in out["specialists"].values()} == {"ok"}
    panel = next(t for t in traces if t["agent"] == "review.panel")
    children = [t for t in traces if t["parent_id"] == panel["id"]]
    assert len(children) == 4 and {t["prompt"] for t in children} == {"review.specialist@v2"}


def test_one_failing_specialist_keeps_the_others(monkeypatch):
    monkeypatch.setattr(llm, "complete_json", specialist_llm(fail="security"))
    out = review.review(build_template("ai-customer-support"))
    assert out["specialists"]["security"]["status"] == "error" and len(out["findings"]) == 3
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: (_ for _ in ()).throw(llm.LLMUnavailable("down")))
    with pytest.raises(llm.LLMUnavailable):
        review.review(build_template("ai-customer-support"))
    with pytest.raises(ValueError):
        review.review(build_template("blank"))


def test_review_endpoint(monkeypatch):
    with new_client(create_app(Store(":memory:")), base_url=BASE) as c:
        signup(c)
        pid = c.post("/api/projects", json={"name": "S", "template": "ai-customer-support"}).json()["id"]
        assert c.post(f"/api/projects/{pid}/ai/review").status_code == 503  # no AI configured
        monkeypatch.setattr(llm, "available", lambda *a, **k: True)
        monkeypatch.setattr(llm, "complete_json", specialist_llm())
        r = c.post(f"/api/projects/{pid}/ai/review")
        assert r.status_code == 200 and r.json()["findings"][0]["fix"] == "Add a replica for Orders DB"
    tracing.set_sink(None)


def test_clarifying_questions_carry_answer_options(monkeypatch):
    replies = [("", [{"name": "ask_clarifying_questions", "arguments": {"questions": [
        {"question": "Which cloud?", "options": ["AWS", "Azure", "GCP", " "]}, "How many users?"]}}]),
        ("I need two answers first.", [])]
    monkeypatch.setattr(llm, "complete_tools", lambda *a, **k: replies.pop(0))
    res = ai.llm_command(build_template("blank"), "design a shop", [])
    assert res.questions == ["Which cloud?", "How many users?"] and res.ops == []
    assert res.question_options == {"Which cloud?": ["AWS", "Azure", "GCP"]}
