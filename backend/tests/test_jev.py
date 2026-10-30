"""The Jev decision API engine: client retries and errors, response parsing, and the comparison panel that runs Jev
and the LLM agent in parallel (Jev scores, the agent explains and gives a second opinion). No network."""

import json
import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import httpx  # noqa: E402
import pytest  # noqa: E402

from archflow import evaluation, jev, llm  # noqa: E402
from archflow.agents import tracing  # noqa: E402

from test_decisions import ALTS, REQS  # noqa: E402

KEYS = {"A": "draft/simple", "B": "draft/scaled"}


def jev_answers(choice: str = "B", score: int = 2) -> dict:
    answers = {"best": {"choice": choice, "probabilities": {"A": 0.3, "B": 0.7}, "confidence": 0.82}}
    for c in evaluation.CRITERIA:
        answers[f"score_{c}_A"] = {"score": 1}
        answers[f"score_{c}_B"] = {"score": score}
    return {"answers": answers, "model": "jev-latest", "usage": {"questions": 15}}


def transport(*responses):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        status, body, headers = responses[min(len(calls), len(responses)) - 1]
        return httpx.Response(status, json=body, headers=headers or {})
    return httpx.MockTransport(handler), calls


def test_call_retries_then_parses():
    t, calls = transport((429, {"message": "slow down"}, {"retry-after": "0"}), (200, jev_answers(), None))
    waits = []
    resp = jev.call({"task": "x"}, jev.build_questions(KEYS, evaluation.CRITERIA, evaluation.SCALE),
                    api_key="k", transport=t, sleep=waits.append)
    assert len(calls) == 2 and waits == [0.0]
    assert calls[0]["questions"]["best"]["type"] == "choice" and len(calls[0]["questions"]) == 1 + 2 * 7
    parsed = jev.parse(resp, KEYS, list(evaluation.CRITERIA))
    assert parsed["recommended"] == "draft/scaled" and parsed["scores"]["draft/scaled"]["cost"] == 3.0  # 0-based
    assert parsed["probabilities"] == {"draft/simple": 0.3, "draft/scaled": 0.7} and parsed["confidence"] == 0.82


def test_rejected_key_is_not_retried_and_says_why():
    t, calls = transport((401, {"code": -1, "message": "Invalid API key."}, None))
    with pytest.raises(jev.JevError, match="HTTP 401 Invalid API key"):
        jev.call({}, {}, api_key="bad", transport=t, sleep=lambda s: None)
    assert len(calls) == 1
    with pytest.raises(jev.JevUnavailable):
        jev.call({}, {}, api_key="")
    with pytest.raises(jev.JevError, match="no usable answers"):
        jev.parse({"answers": {}}, KEYS, ["cost"])


def fake_llm(recommended: str):
    def complete_json(system, messages, schema, **kw):
        row = {c: 3 for c in evaluation.CRITERIA}
        detail = {"reasons": {"cost": "cheap"}, "summary": "s", "strengths": ["x"], "risks": ["y"], "best_when": "z"}
        return {"recommended": recommended, "rationale": "Because of the cache.", "confidence": 0.6,
                "deciding_factors": ["latency"], "flip_conditions": "budget", "follow_up_questions": ["Why?"],
                "scores": [{"alternative": "A", **row, **detail}, {"alternative": "B", **row, **detail}]}
    return complete_json


@pytest.fixture()
def jev_on(monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    t, calls = transport((200, jev_answers("B"), None))
    real = jev.call
    monkeypatch.setattr(jev, "call", lambda state, questions, **kw: real(state, questions, transport=t,
                                                                         sleep=lambda s: None, **kw))
    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    return calls


@pytest.mark.parametrize("llm_pick, agrees", [("B", True), ("A", False)])
def test_panel_runs_jev_and_the_agent_in_parallel(jev_on, monkeypatch, llm_pick, agrees):
    monkeypatch.setattr(llm, "complete_json", fake_llm(llm_pick))
    with tracing.listen(lambda t: None) as traces:
        out = evaluation.evaluate(REQS, ALTS)
    assert out["engine"] == "jev" and out["recommended"] == "draft/scaled" and out["notes"] == []
    assert out["scores"]["draft/scaled"]["cost"] == 3.0 and out["probabilities"]["draft/scaled"] == 0.7
    assert out["details"]["draft/scaled"]["reasons"] == {"cost": "cheap"} and out["follow_up_questions"] == ["Why?"]
    second = out["second_opinion"]
    assert second["agrees"] is agrees and second["recommended"] == KEYS[llm_pick]
    assert ("agrees" in out["rationale"]) is agrees and ("would pick" in out["rationale"]) is not agrees
    by_agent = {t["agent"]: t for t in traces}
    panel = by_agent["compare.panel"]
    assert by_agent["compare.jev"]["parent_id"] == panel["id"] == by_agent["compare.evaluate"]["parent_id"]
    assert by_agent["compare.jev"]["spans"][0]["kind"] == "api"


def test_jev_failure_falls_back_to_the_agent(monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", "bad-key")
    t, _ = transport((401, {"message": "Invalid API key."}, None))
    real = jev.call
    monkeypatch.setattr(jev, "call", lambda state, questions, **kw: real(state, questions, transport=t, **kw))
    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    monkeypatch.setattr(llm, "complete_json", fake_llm("A"))
    out = evaluation.evaluate(REQS, ALTS)
    assert out["engine"] == "llm" and out["recommended"] == "draft/simple" and "second_opinion" not in out
    assert any(n.startswith("Jev failed") and "Invalid API key" in n for n in out["notes"])
