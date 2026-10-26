"""Eval suites: how each kind of case is run and checked.

A case is a dict from a dataset YAML. Shared keys:
    id: unique within the suite
    project: {template: <name>, ops: [...]}   the project state (ops as in ops.apply_ops)
    expect: {...}                              suite-specific checks, below
    ragas: [faithfulness | answer_accuracy | technically_sound | grounded_in_project, ...]   optional

Each runner returns ``Outcome(response, checks, contexts, question, reference)``; ``checks`` are
``(name, passed, detail)`` and the case score is the share that passed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import ai, chat, evaluation, suggestions
from ..analysis import full_analysis
from ..models import ProjectState
from ..ops import apply_ops
from ..templates import build_template

Check = tuple[str, bool, str]


@dataclass
class Outcome:
    response: str
    checks: list[Check] = field(default_factory=list)
    question: str = ""
    contexts: list[str] = field(default_factory=list)
    reference: str = ""
    raw: Any = None


def build(spec: dict[str, Any] | None) -> ProjectState:
    spec = spec or {}
    state = build_template(spec.get("template", "blank"))
    if spec.get("ops"):
        state, _ = apply_ops(state, spec["ops"])
    return state


def _text_checks(text: str, expect: dict[str, Any]) -> list[Check]:
    low = text.lower()
    out: list[Check] = []
    for term in expect.get("mentions_all") or []:
        out.append((f"mentions '{term}'", term.lower() in low, ""))
    if expect.get("mentions_any"):
        terms = expect["mentions_any"]
        hit = [t for t in terms if t.lower() in low]
        out.append((f"mentions one of {terms}", bool(hit), ", ".join(hit)))
    for term in expect.get("not_mentions") or []:
        out.append((f"does not mention '{term}'", term.lower() not in low, ""))
    for pattern in expect.get("forbid") or []:
        m = re.search(pattern, text, re.I)
        out.append((f"avoids /{pattern}/", m is None, m.group(0) if m else ""))
    if expect.get("max_words"):
        n = len(text.split())
        out.append((f"at most {expect['max_words']} words", n <= expect["max_words"], f"{n} words"))
    return out


def project_context(state: ProjectState) -> str:
    """The project block the Ask assistant answers from (as main.project_context, without history)."""
    return (f"<project>\n<model>\n{ai._state_for_prompt(state)}\n</model>\n"
            f"<analysis>\n{json.dumps(full_analysis(state)['health'])}\n</analysis>\n</project>")


# --------------------------------------------------------------------------- suites
def run_compare(case: dict[str, Any], org_id: str | None) -> Outcome:
    alts = {name: build(spec) for name, spec in case["alternatives"].items()}
    reqs = evaluation.merged_requirements(build(case.get("project")), alts)
    ev = evaluation.evaluate(reqs, alts, org_id=org_id)
    exp = case.get("expect") or {}
    checks: list[Check] = [("engine is llm", ev["engine"] == "llm", ev["engine"] + "; " + "; ".join(ev["notes"]))]
    if exp.get("recommended"):
        checks.append((f"recommends {exp['recommended']}", ev["recommended"] == exp["recommended"], ev["recommended"]))
    for better, worse, crit in exp.get("scores_higher") or []:
        a, b = ev["scores"].get(better, {}).get(crit), ev["scores"].get(worse, {}).get(crit)
        checks.append((f"{crit}: {better} > {worse}", a is not None and b is not None and a > b, f"{a} vs {b}"))
    if exp.get("reasons", True):
        missing = [f"{b}.{c}" for b in ev["alternatives"] for c in ev["criteria"]
                   if not (ev.get("details", {}).get(b, {}).get("reasons") or {}).get(c)]
        checks.append(("every score has a reason", not missing, ", ".join(missing[:5])))
    checks.append(("has follow-up questions", bool(ev.get("follow_up_questions")), ""))
    checks += _text_checks(ev["rationale"], exp)
    return Outcome(response=ev["rationale"], checks=checks, question="Which architecture fits the requirements best?",
                   contexts=[json.dumps(evaluation.build_state(reqs, alts))], raw=ev)


def run_ask(case: dict[str, Any], org_id: str | None) -> Outcome:
    state = build(case.get("project"))
    ctx = project_context(state)
    answer = ai.claude_ask(ctx, case["question"], org_id)
    exp = case.get("expect") or {}
    return Outcome(response=answer, checks=[("answered", bool(answer.strip()), "")] + _text_checks(answer, exp),
                   question=case["question"], contexts=[ctx], reference=case.get("reference", ""))


def run_command(case: dict[str, Any], org_id: str | None) -> Outcome:
    state = build(case.get("project"))
    res = ai.llm_command(state, case["request"], case.get("selection") or [], org_id)
    exp = case.get("expect") or {}
    checks: list[Check] = []
    try:
        after, summary = apply_ops(state, res.ops)
        checks.append(("ops apply cleanly", True, f"{len(res.ops)} op(s)"))
    except Exception as exc:  # noqa: BLE001
        return Outcome(response=res.explanation, checks=[("ops apply cleanly", False, str(exc))], raw=res.ops)
    added = [after.nodes[i] for i in set(after.nodes) - set(state.nodes)]
    removed = [state.nodes[i].name for i in set(state.nodes) - set(after.nodes)]
    for t in exp.get("adds_types") or []:
        checks.append((f"adds a {t}", any(n.type == t for n in added), ", ".join(f"{n.name}({n.type})" for n in added)))
    if exp.get("adds_any"):
        names = " ".join(f"{n.name} {n.technology}" for n in added).lower()
        checks.append((f"adds one of {exp['adds_any']}", any(x.lower() in names for x in exp["adds_any"]), names))
    if exp.get("removes_nothing"):
        checks.append(("removes nothing", not removed, ", ".join(removed)))
    def ids(ref: str) -> set[str]:
        """Components matching ``ref``: exact name, else a name or technology containing it ("Redis")."""
        ref = ref.lower()
        exact = {n.id for n in after.nodes.values() if n.name.lower() == ref}
        return exact or {n.id for n in after.nodes.values() if ref in n.name.lower() or ref in n.technology.lower()}

    for src, dst in exp.get("connects") or []:
        s, d = ids(src), ids(dst)
        ok = any(e.source in s and e.target in d for e in after.edges.values())
        checks.append((f"connects {src} -> {dst}", ok, ""))
    if "max_ops" in exp:
        checks.append((f"at most {exp['max_ops']} ops", len(res.ops) <= exp["max_ops"], str(len(res.ops))))
    checks.append(("explains the change", bool(res.explanation.strip()), ""))
    return Outcome(response=res.explanation + "\n" + "\n".join(summary), checks=checks, question=case["request"],
                   contexts=[ai._state_for_prompt(state)], raw=res.ops)


def run_design(case: dict[str, Any], org_id: str | None) -> Outcome:
    state = build(case.get("project"))
    turns = case.get("turns") or [{"role": "user", "content": case["idea"]}]
    out = chat.llm_turn(turns, state, org_id)
    card = out.get("card") or {}
    exp = case.get("expect") or {}
    checks: list[Check] = []
    if exp.get("action"):
        kind = {"question": "ask", "architectures": "propose"}.get(card.get("type"), card.get("type"))
        checks.append((f"action {exp['action']}", kind == exp["action"], str(kind)))
    if card.get("type") == "question":
        checks.append(("question has 2+ options", len(card.get("options") or []) >= 2, ""))
        text = card.get("question", "") + " " + " ".join(o["label"] for o in card.get("options") or [])
    else:
        options = card.get("options") or []
        checks.append((f"at least {exp.get('min_options', 2)} options", len(options) >= exp.get("min_options", 2),
                       str(len(options))))
        for t in exp.get("component_types_all") or []:
            checks.append((f"every option has a {t}", bool(options) and all(
                any(c["type"] == t for c in o["components"]) for o in options), ""))
        text = " ".join(f"{o['name']} {o['summary']} " + " ".join(c["name"] + " " + c["technology"]
                                                                  for c in o["components"]) for o in options)
    checks += _text_checks(text, exp)
    return Outcome(response=text, checks=checks, question=turns[-1]["content"], raw=out)


def run_suggestions(case: dict[str, Any], org_id: str | None) -> Outcome:
    state = build(case.get("project"))
    items = suggestions.suggest(state, case.get("mode", "edit"), selection=case.get("selection"),
                                last_question=case.get("last_question", ""), last_answer=case.get("last_answer", ""),
                                org_id=org_id)
    exp = case.get("expect") or {}
    names = [n.name.lower() for n in state.nodes.values() if n.type != "note"] + [r.lower() for r in state.requirements]
    checks: list[Check] = [("3-4 suggestions", 3 <= len(items) <= 4, str(len(items)))]
    if names:
        grounded = [s["label"] for s in items if any(n in (s["label"] + " " + s["prompt"]).lower() for n in names)]
        share = len(grounded) / len(items) if items else 0
        checks.append(("most name a real component or requirement", share >= exp.get("grounded_share", 0.75),
                       f"{share:.0%}"))
    text = "\n".join(f"- {s['label']}: {s['prompt']}" for s in items)
    checks += _text_checks(text, exp)
    return Outcome(response=text, checks=checks, question=f"Suggest next steps ({case.get('mode', 'edit')})",
                   contexts=[json.dumps(suggestions.project_summary(state))], raw=items)


SUITES: dict[str, tuple[Callable[[dict[str, Any], str | None], Outcome], list[str]]] = {
    # suite: (runner, prompt ids it exercises)
    "compare": (run_compare, ["compare.evaluate"]),
    "ask": (run_ask, ["assistant.ask"]),
    "command": (run_command, ["canvas.command"]),
    "design": (run_design, ["design.turn"]),
    "suggestions": (run_suggestions, ["assistant.suggestions"]),
}
