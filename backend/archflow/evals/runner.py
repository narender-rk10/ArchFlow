"""``archflow-eval``: run the eval datasets against the configured models and write a report.

    uv run archflow-eval                                  # every suite
    uv run archflow-eval --suite compare --suite ask      # some suites
    uv run archflow-eval --prompt compare.evaluate=v2 --baseline evals/reports/last.json   # A/B a prompt version
    uv run --with ragas==0.3.9 --with "langchain-community>=0.3,<0.4" \
        archflow-eval --ragas --fail-under 0.8            # add RAGAS metrics; non-zero exit below 80%

Needs a provider key (GEMINI_API_KEY, ANTHROPIC_API_KEY or OPENAI_API_KEY). RAGAS is kept out of the lockfile
(its pins would hold back production dependencies), hence ``--with``. The report (JSON) holds, per case: checks,
score, RAGAS metrics, cost, tokens, latency and the prompt versions that ran; and per suite the mean score,
pass rate and cost.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .. import llm
from ..agents import prompts, tracing
from . import ragas_judge
from .suites import SUITES

DATASETS_DIR = Path(__file__).resolve().parent / "datasets"
log = logging.getLogger("archflow.evals")


def load_suites(directory: Path, only: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        suite = data.get("suite") or path.stem
        if suite not in SUITES:
            raise SystemExit(f"{path.name}: unknown suite '{suite}' (known: {', '.join(SUITES)})")
        if only and suite not in only:
            continue
        out.setdefault(suite, []).extend(data.get("cases") or [])
    return out


def run_case(suite: str, case: dict[str, Any], *, use_ragas: bool, judge: Any, org_id: str | None) -> dict[str, Any]:
    runner, _ = SUITES[suite]
    t0 = time.monotonic()
    with tracing.listen(lambda t: None) as traces, llm.usage_scope(feature="eval"):
        try:
            outcome = runner(case, org_id)
            error = ""
        except Exception as exc:  # noqa: BLE001 - a failing case is reported, not fatal
            log.exception("case %s/%s failed", suite, case.get("id"))
            outcome, error = None, f"{type(exc).__name__}: {exc}"
    result: dict[str, Any] = {
        "suite": suite, "id": case.get("id", "?"), "error": error,
        "latency_ms": int((time.monotonic() - t0) * 1000),
        # A parent run's totals include its children (agents fired in parallel): count top-level runs only.
        "cost_usd": round(sum(t["cost_usd"] for t in traces if not t.get("parent_id")), 6),
        "tokens": sum(t["prompt_tokens"] + t["completion_tokens"] for t in traces if not t.get("parent_id")),
        "prompts": sorted({t["prompt"] for t in traces if not t["prompt"].endswith(("@parallel", "@api"))}),
        "tool_calls": sum(t["tool_calls"] for t in traces),
    }
    if outcome is None:
        result.update(checks=[], score=0.0, passed=False, ragas={})
        return result
    checks = [{"check": n, "passed": bool(ok), "detail": d} for n, ok, d in outcome.checks]
    score = sum(c["passed"] for c in checks) / len(checks) if checks else 0.0
    result.update(checks=checks, score=round(score, 3), passed=all(c["passed"] for c in checks),
                  response=outcome.response[:4000], ragas={})
    wanted = case.get("ragas") or []
    if use_ragas and wanted:
        unknown = [m for m in wanted if m not in ragas_judge.METRICS]
        if unknown:
            raise SystemExit(f"{suite}/{result['id']}: unknown ragas metric(s) {unknown} ({', '.join(ragas_judge.METRICS)})")
        with llm.usage_scope(feature="eval"), tracing.listen(lambda t: None):
            result["ragas"] = ragas_judge.score(outcome.question, outcome.response, outcome.contexts,
                                                reference=outcome.reference, metrics_wanted=wanted, judge_llm=judge)
    return result


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for suite in sorted({r["suite"] for r in results}):
        rows = [r for r in results if r["suite"] == suite]
        metrics: dict[str, list[float]] = {}
        for r in rows:
            for k, v in (r.get("ragas") or {}).items():
                if v is not None:
                    metrics.setdefault(k, []).append(v)
        out[suite] = {"cases": len(rows), "score": round(sum(r["score"] for r in rows) / len(rows), 3),
                      "pass_rate": round(sum(r["passed"] for r in rows) / len(rows), 3),
                      "errors": sum(1 for r in rows if r["error"]),
                      "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
                      "latency_ms_p50": sorted(r["latency_ms"] for r in rows)[len(rows) // 2],
                      "ragas": {k: round(sum(v) / len(v), 3) for k, v in metrics.items()}}
    return out


def _delta(now: float, before: float | None) -> str:
    if before is None:
        return ""
    d = now - before
    return f" ({'+' if d >= 0 else ''}{d:.3f})"


def print_report(report: dict[str, Any], baseline: dict[str, Any] | None) -> None:
    base = (baseline or {}).get("summary", {})
    print(f"\nArchFlow eval · {report['created_at']} · models: {', '.join(report['models']) or '?'}")
    print(f"prompts: {', '.join(report['prompt_versions'])}\n")
    print(f"{'suite':<12} {'cases':>5} {'score':>14} {'pass':>14} {'cost $':>8}  ragas")
    for suite, s in report["summary"].items():
        b = base.get(suite, {})
        ragas = ", ".join(f"{k} {v:.2f}{_delta(v, b.get('ragas', {}).get(k))}" for k, v in s["ragas"].items())
        print(f"{suite:<12} {s['cases']:>5} {s['score']:>6.3f}{_delta(s['score'], b.get('score')):>8} "
              f"{s['pass_rate']:>6.3f}{_delta(s['pass_rate'], b.get('pass_rate')):>8} {s['cost_usd']:>8.4f}  {ragas}")
    failed = [r for r in report["results"] if not r["passed"]]
    if failed:
        print("\nFailed checks:")
        for r in failed:
            bad = [c["check"] + (f" [{c['detail']}]" if c["detail"] else "") for c in r["checks"] if not c["passed"]]
            print(f"  {r['suite']}/{r['id']}: {r['error'] or '; '.join(bad)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="archflow-eval", description=__doc__.split("\n\n")[0])
    parser.add_argument("--suite", action="append", help=f"suite to run (repeatable): {', '.join(SUITES)}")
    parser.add_argument("--case", action="append", help="run only these case ids")
    parser.add_argument("--datasets", type=Path, default=DATASETS_DIR, help="directory of dataset YAML files")
    parser.add_argument("--prompt", action="append", default=[], metavar="ID=VERSION",
                        help="pin a prompt version for this run (repeatable)")
    parser.add_argument("--ragas", action="store_true", help="add RAGAS metrics (run via uv run --with ragas==0.3.9 ...)")
    parser.add_argument("--judge-group", default="fast", help="model group of the RAGAS judge (fast or reasoning)")
    parser.add_argument("--out", type=Path, help="report path (default evals/reports/<timestamp>.json)")
    parser.add_argument("--baseline", type=Path, help="earlier report to compare against")
    parser.add_argument("--fail-under", type=float, help="exit 1 when any suite's mean score is below this")
    parser.add_argument("--org", help="organization id whose AI settings and keys to use")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    pins = dict(p.split("=", 1) for p in args.prompt)
    for pid, v in pins.items():
        prompts.get(pid, v)  # fail fast on a typo
    prompts.set_run_overrides(pins)
    if not llm.available(args.org):
        print("No AI provider is configured: set GEMINI_API_KEY, ANTHROPIC_API_KEY or OPENAI_API_KEY.", file=sys.stderr)
        return 2
    if args.ragas and not ragas_judge.available():
        print("RAGAS is not installed. Run: uv run --with ragas==0.3.9 --with 'langchain-community>=0.3,<0.4' "
              "archflow-eval --ragas", file=sys.stderr)
        return 2
    judge = ragas_judge.judge(args.judge_group, args.org) if args.ragas else None
    suites = load_suites(args.datasets, args.suite)
    results = []
    for suite, cases in suites.items():
        for case in cases:
            if args.case and case.get("id") not in args.case:
                continue
            print(f"· {suite}/{case.get('id')}", file=sys.stderr, flush=True)
            results.append(run_case(suite, case, use_ragas=args.ragas, judge=judge, org_id=args.org))
    used = sorted({p for r in results for p in r["prompts"]})
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "models": [llm.primary_model("reasoning"), llm.primary_model("fast")],
        "prompt_versions": used or [f"{p}@{prompts.active_version(p)}" for s in suites for p in SUITES[s][1]],
        "pins": pins, "ragas": bool(args.ragas),
        "summary": summarize(results) if results else {}, "results": results,
    }
    out = args.out or (Path("evals/reports") / f"{report['created_at'].replace(':', '')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
    print_report(report, baseline)
    print(f"\nreport: {out}")
    prompts.set_run_overrides({})
    if args.fail_under is not None and any(s["score"] < args.fail_under for s in report["summary"].values()):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
