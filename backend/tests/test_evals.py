"""The eval harness (archflow-eval): datasets load and build, checks score cases, reports compare to a baseline.
The model is faked; RAGAS metrics are tested only when ragas is installed."""

import json
import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import pytest  # noqa: E402

from archflow import llm  # noqa: E402
from archflow.evals import ragas_judge, runner  # noqa: E402
from archflow.evals.suites import SUITES, build  # noqa: E402


def test_bundled_datasets_load_and_build():
    suites = runner.load_suites(runner.DATASETS_DIR)
    assert set(suites) == set(SUITES)
    for cases in suites.values():
        assert len({c["id"] for c in cases}) == len(cases) >= 3
        for c in cases:
            build(c.get("project"))
            for spec in (c.get("alternatives") or {}).values():
                build(spec)


DATASET = """
suite: compare
cases:
  - id: picks-cache
    alternatives:
      draft/a: {ops: [{op: add_node, node: {name: App, type: service}}]}
      draft/b: {ops: [{op: add_node, node: {name: App, type: service}}, {op: add_node, node: {name: Cache, type: cache, technology: Redis}}]}
    expect: {recommended: draft/b, scores_higher: [[draft/b, draft/a, performance]], mentions_any: [cache]}
  - id: expects-the-wrong-one
    alternatives:
      draft/a: {ops: [{op: add_node, node: {name: App, type: service}}]}
      draft/b: {ops: [{op: add_node, node: {name: App, type: service}}]}
    expect: {recommended: draft/a}
"""


def fake_evaluator(system, messages, schema, **kw):
    llm._record(None, "reasoning", "gemini-test", 500, 100, 5.0)
    row = {c: 3 for c in ("cost", "scalability", "performance", "security", "reliability", "operability",
                          "delivery_speed")}
    detail = {"reasons": {c: "because" for c in row}, "summary": "s", "strengths": ["x"], "risks": ["y"],
              "best_when": "z"}
    return {"recommended": "B", "rationale": "B has a cache in front of the app.", "confidence": 0.8,
            "deciding_factors": ["latency"], "flip_conditions": "budget", "follow_up_questions": ["Why?"],
            "scores": [{"alternative": "A", **row, **detail}, {"alternative": "B", **row, "performance": 5, **detail}]}


def test_runner_scores_cases_and_reports(tmp_path, monkeypatch, capsys):
    (tmp_path / "compare.yaml").write_text(DATASET, encoding="utf-8")
    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    monkeypatch.setattr(llm, "complete_json", fake_evaluator)
    out = tmp_path / "report.json"
    assert runner.main(["--datasets", str(tmp_path), "--out", str(out), "--prompt", "compare.evaluate=v1"]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    good, bad = report["results"]
    assert good["passed"] and good["score"] == 1.0 and good["prompts"] == ["compare.evaluate@v1"]
    assert good["cost_usd"] > 0 and good["tokens"] == 600
    assert not bad["passed"] and any(c["check"] == "recommends draft/a" and not c["passed"] for c in bad["checks"])
    s = report["summary"]["compare"]
    assert s["cases"] == 2 and s["pass_rate"] == 0.5 and report["pins"] == {"compare.evaluate": "v1"}
    assert "compare.evaluate@v1" in report["prompt_versions"]

    # an A/B rerun against the first report prints deltas, and --fail-under gates CI
    out2 = tmp_path / "report2.json"
    assert runner.main(["--datasets", str(tmp_path), "--out", str(out2), "--baseline", str(out),
                        "--fail-under", "0.99"]) == 1
    printed = capsys.readouterr().out
    assert "(+0.000)" in printed and "expects-the-wrong-one" in printed


def test_runner_needs_a_model(tmp_path, monkeypatch):
    (tmp_path / "compare.yaml").write_text(DATASET, encoding="utf-8")
    monkeypatch.setattr(llm, "available", lambda *a, **k: False)
    assert runner.main(["--datasets", str(tmp_path)]) == 2


def test_a_crashing_case_is_reported(tmp_path, monkeypatch):
    (tmp_path / "compare.yaml").write_text(DATASET, encoding="utf-8")
    monkeypatch.setattr(llm, "available", lambda *a, **k: True)

    def boom(*a, **k):
        raise llm.LLMInvalidOutput("bad json twice")

    monkeypatch.setattr(llm, "complete_json", boom)
    out = tmp_path / "r.json"
    runner.main(["--datasets", str(tmp_path), "--out", str(out), "--case", "picks-cache"])
    (r,) = json.loads(out.read_text(encoding="utf-8"))["results"]
    # the evaluator falls back to the heuristic engine, so the "engine is llm" check fails the case
    assert not r["passed"] and any(c["check"] == "engine is llm" and not c["passed"] for c in r["checks"])


@pytest.mark.skipif(not ragas_judge.available(), reason="ragas not installed (uv run --with ragas==0.3.9 ...)")
def test_ragas_metrics_use_the_gateway(monkeypatch):
    calls = []

    def judge(system, messages, tools, **kw):
        calls.append(messages[0]["content"])
        return '{"statements": []}', []

    monkeypatch.setattr(llm, "complete_tools", judge)
    scores = ragas_judge.score("q", "a", ["ctx"], metrics_wanted=["faithfulness", "technically_sound"])
    assert set(scores) == {"faithfulness", "groundedness", "technically_sound"} and calls
