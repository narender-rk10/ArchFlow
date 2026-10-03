"""Evaluate 2–4 architecture alternatives (draft branches) against the project's requirements.

Three engines, tried in order; the result always says which one produced it (``engine``):

1. ``"jev"``: the Jev decision API (``POST https://thejevai.com/v1/systemone``) when ``JEV_API_KEY`` is set.
   One ``choice`` question ("which alternative best satisfies the requirements?") plus one ``score``
   question per (criterion, alternative) on a 1–5 scale.
2. ``"llm"``: the LLM gateway (``llm.complete_json``) with an equivalent JSON schema.
3. ``"heuristic"``: a deterministic offline estimate from the model's structure (component counts,
   caches, queues, auth, replicas) and ``analysis.health``.

Result shape::

    {"engine": "jev" | "llm" | "heuristic", "recommended": <branch>, "alternatives": [<branch>, ...],
     "criteria": [...], "scores": {branch: {criterion: number 1..5}}, "totals": {branch: number},
     "rationale": str, "confidence": float | None, "notes": [why earlier engines were skipped], "raw": ...}

Environment: JEV_API_KEY, JEV_API_URL (default https://thejevai.com/v1/systemone), JEV_MODEL (jev-latest),
JEV_TIMEOUT seconds (60), JEV_MAX_ATTEMPTS (4), JEV_BACKOFF_BASE (0.5), JEV_BACKOFF_CAP (20).
The API key is never logged.
"""

from __future__ import annotations

import email.utils
import json
import logging
import os
import random
import re
import time
from typing import Any, Callable

import httpx

from . import llm
from .analysis import health, validate_architecture
from .models import ProjectState

log = logging.getLogger("archflow.jev")

DEFAULT_URL = "https://thejevai.com/v1/systemone"
ENGINES = ("jev", "llm", "heuristic")

# Criterion -> what a 5 means. Every scale is "higher is better" (cost 5 = cheapest).
CRITERIA: dict[str, str] = {
    "cost": "Cost efficiency: infrastructure, licensing and running cost (5 = cheapest to build and run)",
    "scalability": "Scalability: ability to handle growth in users, data and traffic (5 = scales furthest with least rework)",
    "performance": "Performance: latency and throughput for the main user flows (5 = fastest)",
    "security": "Security: authentication, authorization, data protection, attack surface (5 = strongest posture)",
    "reliability": "Reliability: availability, fault tolerance, recovery from failure (5 = most resilient)",
    "operability": "Operability: ease of deploying, monitoring, debugging and maintaining (5 = easiest to operate)",
    "delivery_speed": "Delivery speed: how quickly a typical team can build and ship it (5 = fastest to deliver)",
}
CRITERIA_LABELS = {k: v.split(":")[0] for k, v in CRITERIA.items()}
SCALE = [
    "1 - Very poor: clearly fails this criterion for the stated requirements",
    "2 - Poor: significant gaps or risks",
    "3 - Adequate: acceptable with some trade-offs",
    "4 - Good: meets the requirements well",
    "5 - Excellent: best-in-class for these requirements",
]
# NFR categories that raise a criterion's weight when ranking alternatives.
NFR_WEIGHTS = {"cost": "cost", "scalability": "scalability", "performance": "performance", "latency": "performance",
               "security": "security", "privacy": "security", "availability": "reliability",
               "reliability": "reliability", "observability": "operability", "maintainability": "operability"}


class JevError(RuntimeError):
    """The Jev API failed (after retries) or returned something unusable."""


class JevUnavailable(JevError):
    """No JEV_API_KEY configured."""


# --------------------------------------------------------------------------- state building
def architecture_summary(state: ProjectState, limit: int = 80) -> dict[str, Any]:
    """A compact, model-friendly description of one alternative's architecture."""
    names = {n.id: n.name for n in state.nodes.values()}
    comps = []
    for n in list(state.nodes.values())[:limit]:
        if n.type == "note":
            continue
        c: dict[str, Any] = {"name": n.name, "type": n.type}
        if n.technology:
            c["technology"] = n.technology
        if n.vendor:
            c["vendor"] = n.vendor
        if n.description:
            c["description"] = n.description[:200]
        if n.props:
            c["props"] = dict(list(n.props.items())[:6])
        comps.append(c)
    conns = []
    for e in list(state.edges.values())[: limit * 2]:
        line = f"{names.get(e.source, e.source)} -> {names.get(e.target, e.target)}"
        if e.protocol or e.label:
            line += f" ({e.protocol or e.label})"
        conns.append(line)
    return {"components": comps, "connections": conns,
            "counts": {"components": len(comps), "connections": len(state.edges)}}


def requirements_summary(state: ProjectState) -> list[dict[str, Any]]:
    out = []
    for r in state.requirements.values():
        item: dict[str, Any] = {"id": r.id, "title": r.title, "kind": r.kind, "priority": r.priority}
        if r.category:
            item["category"] = r.category
        if r.description:
            item["description"] = r.description[:400]
        if r.acceptance_criteria:
            item["acceptance_criteria"] = r.acceptance_criteria[:6]
        out.append(item)
    return out


def merged_requirements(base: ProjectState | None, alternatives: dict[str, ProjectState]) -> ProjectState:
    """Requirements from the base branch plus any extra ones that only exist on an alternative."""
    out = ProjectState()
    for st in [base, *alternatives.values()]:
        if st is None:
            continue
        for rid, r in st.requirements.items():
            out.requirements.setdefault(rid, r)
    return out


def option_keys(branches: list[str]) -> dict[str, str]:
    """Stable, API-safe option keys (A, B, C, D) for branch names."""
    return {chr(ord("A") + i): b for i, b in enumerate(branches)}


def build_state(requirements: ProjectState, alternatives: dict[str, ProjectState]) -> dict[str, Any]:
    keys = option_keys(list(alternatives))
    return {
        "task": "Compare candidate software architectures for one system and judge them against its requirements.",
        "requirements": requirements_summary(requirements),
        "alternatives": {k: {"branch": b, **architecture_summary(alternatives[b])} for k, b in keys.items()},
    }


def build_questions(branches: list[str]) -> dict[str, Any]:
    keys = option_keys(branches)
    questions: dict[str, Any] = {
        "best": {
            "type": "choice",
            "instructions": "Which alternative architecture best satisfies the requirements, including the "
                            "non-functional requirements (NFRs)? Weigh critical and high-priority requirements most.",
            "criteria": {k: f"Alternative {k} (branch '{b}')" for k, b in keys.items()},
        }
    }
    for crit, desc in CRITERIA.items():
        for k, b in keys.items():
            questions[f"score_{crit}_{k}"] = {
                "type": "score",
                "instructions": f"Rate alternative {k} (branch '{b}') on {desc}, given the requirements.",
                "criteria": SCALE,
            }
    return questions


def build_payload(requirements: ProjectState, alternatives: dict[str, ProjectState]) -> dict[str, Any]:
    return {"state": build_state(requirements, alternatives), "model": os.environ.get("JEV_MODEL", "jev-latest"),
            "questions": build_questions(list(alternatives))}


# --------------------------------------------------------------------------- HTTP client
def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        parsed = email.utils.parsedate_tz(value)
        return max(0.0, email.utils.mktime_tz(parsed) - time.time()) if parsed else None


def _backoff(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with full jitter."""
    return random.uniform(0, min(cap, base * (2 ** attempt)))


def _retryable(status: int) -> bool:
    return status in (408, 429, 529) or status >= 500


def call_jev(payload: dict[str, Any], *, api_key: str | None = None, url: str | None = None,
             transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
             max_attempts: int | None = None) -> dict[str, Any]:
    """POST the payload to the Jev API with retries on 408/429/529/5xx and network errors."""
    key = api_key or os.environ.get("JEV_API_KEY", "")
    if not key:
        raise JevUnavailable("JEV_API_KEY is not set")
    url = url or os.environ.get("JEV_API_URL", DEFAULT_URL)
    attempts = max(1, max_attempts or int(os.environ.get("JEV_MAX_ATTEMPTS", 4)))
    base = float(os.environ.get("JEV_BACKOFF_BASE", 0.5))
    cap = float(os.environ.get("JEV_BACKOFF_CAP", 20))
    timeout = float(os.environ.get("JEV_TIMEOUT", 60))
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    last = ""
    with httpx.Client(timeout=timeout, transport=transport) as client:
        for attempt in range(attempts):
            wait: float | None = None
            try:
                resp = client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last = f"network error: {type(exc).__name__}"
            else:
                if resp.status_code < 300:
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        raise JevError("Jev returned invalid JSON") from exc
                    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
                        raise JevError("Jev response has no 'answers' object")
                    return data
                last = f"HTTP {resp.status_code}"
                if not _retryable(resp.status_code):
                    detail = resp.text[:300]
                    raise JevError(f"Jev request failed: HTTP {resp.status_code} {detail}".strip())
                wait = _retry_after(resp)
            if attempt + 1 < attempts:
                delay = min(cap, wait) if wait is not None else _backoff(attempt, base, cap)
                log.info("jev attempt %d failed (%s); retrying in %.2fs", attempt + 1, last, delay)
                sleep(delay)
    raise JevError(f"Jev request failed after {attempts} attempts ({last})")


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_response(resp: dict[str, Any], branches: list[str]) -> dict[str, Any]:
    """Turn a Jev response into the common result shape (engine 'jev')."""
    keys = option_keys(branches)
    answers = resp.get("answers") or {}
    best = answers.get("best") or {}
    choice = best.get("choice")
    if choice not in keys:
        # tolerate an answer that names the branch instead of the option key
        rev = {b: k for k, b in keys.items()}
        choice = rev.get(choice)
    probs = {keys[k]: round(p, 3) for k, v in (best.get("probabilities") or {}).items()
             if k in keys and (p := _num(v)) is not None}
    scores: dict[str, dict[str, float]] = {b: {} for b in branches}
    for crit in CRITERIA:
        for k, b in keys.items():
            ans = answers.get(f"score_{crit}_{k}") or {}
            s = _num(ans.get("score"))
            if s is None:
                continue
            # Jev scores are 0-based indexes into the criteria levels (0..4); our scale is 1..5.
            scores[b][crit] = round(min(5.0, max(1.0, s + 1)), 2)
    if not any(scores.values()) and choice is None:
        raise JevError("Jev response contained no usable answers")
    recommended = keys[choice] if choice else None
    out = _finish("jev", branches, scores, recommended, "", None)
    conf = _num(best.get("confidence"))
    out["confidence"] = round(conf, 3) if conf is not None else None
    out["probabilities"] = probs
    pct = f" ({round(100 * probs[out['recommended']])}% probability)" if out["recommended"] in probs else ""
    out["rationale"] = (f"Jev picked '{out['recommended']}' as the best fit for the requirements{pct}. "
                        + _strengths_text(out))
    out["raw"] = {"model": resp.get("model"), "usage": resp.get("usage"), "elapsed": resp.get("elapsed")}
    return out


# --------------------------------------------------------------------------- scoring helpers
def criterion_weights(requirements: ProjectState) -> dict[str, float]:
    w = {c: 1.0 for c in CRITERIA}
    for r in requirements.requirements.values():
        if r.kind != "non_functional":
            continue
        crit = NFR_WEIGHTS.get(r.category.lower().strip())
        if crit:
            w[crit] += {"critical": 1.5, "high": 1.0, "medium": 0.5, "low": 0.25}.get(r.priority, 0.5)
    return w


def _finish(engine: str, branches: list[str], scores: dict[str, dict[str, float]], recommended: str | None,
            rationale: str, weights: dict[str, float] | None) -> dict[str, Any]:
    w = weights or {c: 1.0 for c in CRITERIA}
    totals: dict[str, float] = {}
    for b in branches:
        vals = [(scores[b][c], w[c]) for c in CRITERIA if c in scores.get(b, {})]
        totals[b] = round(sum(v * wt for v, wt in vals) / sum(wt for _, wt in vals), 2) if vals else 0.0
    if recommended not in branches:
        recommended = max(branches, key=lambda b: (totals[b], -branches.index(b)))
    return {"engine": engine, "recommended": recommended, "alternatives": list(branches),
            "criteria": list(CRITERIA), "criteria_labels": CRITERIA_LABELS, "scores": scores, "totals": totals,
            "rationale": rationale, "confidence": None, "notes": []}


def _strengths_text(result: dict[str, Any]) -> str:
    rec = result["recommended"]
    s = result["scores"].get(rec, {})
    if not s:
        return ""
    best = [CRITERIA_LABELS[c].lower() for c, v in s.items() if v >= 4]
    weak = [CRITERIA_LABELS[c].lower() for c, v in s.items() if v <= 2]
    parts = []
    if best:
        parts.append(f"Strongest on {', '.join(best)}.")
    if weak:
        parts.append(f"Weakest on {', '.join(weak)}.")
    return " ".join(parts)


# --------------------------------------------------------------------------- engines
def evaluate_jev(requirements: ProjectState, alternatives: dict[str, ProjectState], **kw: Any) -> dict[str, Any]:
    branches = list(alternatives)
    resp = call_jev(build_payload(requirements, alternatives), **kw)
    out = parse_response(resp, branches)
    # Jev's choice is authoritative for 'recommended'; totals use the NFR weights for display.
    weighted = _finish("jev", branches, out["scores"], out["recommended"], "", criterion_weights(requirements))
    out["totals"] = weighted["totals"]
    return out


LLM_SYSTEM = """You are a principal software architect. Compare the candidate architectures (alternatives) for one \
system against its requirements, especially the non-functional requirements. Score every alternative on every \
criterion from 1 to 5 where higher is always better (for cost, 5 means cheapest). Be concrete and fair; base the \
scores only on the components and connections given. Pick the alternative that best satisfies the requirements."""


def _llm_schema(keys: list[str]) -> dict[str, Any]:
    crit_props = {c: {"type": "integer", "minimum": 1, "maximum": 5} for c in CRITERIA}
    return {
        "type": "object",
        "properties": {
            "recommended": {"type": "string", "enum": keys},
            "rationale": {"type": "string"},
            "scores": {"type": "array", "items": {
                "type": "object",
                "properties": {"alternative": {"type": "string", "enum": keys}, **crit_props},
                "required": ["alternative", *CRITERIA],
            }},
        },
        "required": ["recommended", "rationale", "scores"],
    }


def evaluate_llm(requirements: ProjectState, alternatives: dict[str, ProjectState],
                 org_id: str | None = None) -> dict[str, Any]:
    branches = list(alternatives)
    keys = option_keys(branches)
    legend = "\n".join(f"- {c}: {d}" for c, d in CRITERIA.items())
    scale = "\n".join(SCALE)
    prompt = (f"<criteria>\n{legend}\n</criteria>\n<scale>\n{scale}\n</scale>\n"
              f"<input>\n{json.dumps(build_state(requirements, alternatives), indent=1)}\n</input>\n"
              f"Return scores for every alternative ({', '.join(keys)}), the recommended alternative key and a "
              "short rationale (3-6 sentences) naming the requirements that decided it.")
    data = llm.complete_json(LLM_SYSTEM, [{"role": "user", "content": prompt}], _llm_schema(list(keys)),
                             group="reasoning", org_id=org_id, max_tokens=4000)
    scores: dict[str, dict[str, float]] = {b: {} for b in branches}
    for row in data.get("scores") or []:
        b = keys.get(row.get("alternative"))
        if not b:
            continue
        for c in CRITERIA:
            v = _num(row.get(c))
            if v is not None:
                scores[b][c] = float(min(5, max(1, round(v))))
    out = _finish("llm", branches, scores, keys.get(data.get("recommended")), str(data.get("rationale", "")).strip(),
                  criterion_weights(requirements))
    if not out["rationale"]:
        out["rationale"] = _strengths_text(out)
    return out


_CACHE_RE = re.compile(r"redis|memcache|cache|cdn|cloudfront|valkey", re.I)
_QUEUE_RE = re.compile(r"kafka|rabbit|sqs|sns|pub/?sub|queue|event ?bus|kinesis|nats|service bus", re.I)
_AUTH_RE = re.compile(r"auth|identity|cognito|keycloak|oauth|okta|iam|entra|sso|waf", re.I)
_OBS_RE = re.compile(r"monitor|observab|grafana|prometheus|datadog|logging|tracing|otel|cloudwatch|sentry", re.I)
_REPLICA_RE = re.compile(r"replica|cluster|multi-?az|ha\b|failover|autoscal|load balancer|\blb\b", re.I)


def _features(state: ProjectState) -> dict[str, Any]:
    nodes = [n for n in state.nodes.values() if n.type != "note"]
    text = lambda n: f"{n.name} {n.technology} {n.description} {' '.join(f'{k} {v}' for k, v in n.props.items())}"  # noqa: E731
    types = [n.type for n in nodes]
    replicas = 0
    for n in nodes:
        r = _num(n.props.get("replicas")) if n.props else None
        if (r and r > 1) or _REPLICA_RE.search(text(n)):
            replicas += 1
    issues = validate_architecture(state)
    return {
        "n": len(nodes), "edges": len(state.edges),
        "cache": "cache" in types or any(_CACHE_RE.search(text(n)) for n in nodes),
        "queue": "queue" in types or any(_QUEUE_RE.search(text(n)) for n in nodes),
        "auth": "security" in types or any(_AUTH_RE.search(text(n)) for n in nodes),
        "gateway": "gateway" in types,
        "obs": any(_OBS_RE.search(text(n)) for n in nodes),
        "replicas": replicas,
        "services": types.count("service") + types.count("agent"),
        "datastores": types.count("database") + types.count("storage"),
        "managed": sum(1 for n in nodes if n.vendor in ("aws", "azure", "gcp", "saas")),
        "errors": sum(1 for i in issues if i["severity"] == "error"),
        "arch_score": health(state)["architecture_score"],
    }


def _clamp(v: float) -> float:
    return round(min(5.0, max(1.0, v)), 1)


def heuristic_scores(state: ProjectState) -> dict[str, float]:
    f = _features(state)
    n, quality = f["n"], f["arch_score"] / 100
    managed_share = f["managed"] / n if n else 0
    return {
        "cost": _clamp(5 - max(0, n - 4) * 0.25 - f["replicas"] * 0.3 - (0.3 if f["queue"] else 0)),
        "scalability": _clamp(1.5 + 1.0 * f["cache"] + 1.0 * f["queue"] + 0.5 * min(f["replicas"], 2)
                              + 0.5 * f["gateway"] + 0.5 * (f["services"] > 1)),
        "performance": _clamp(2.0 + 1.5 * f["cache"] + 0.5 * f["queue"] + 0.3 * min(f["replicas"], 2)
                              - 0.1 * max(0, n - 10)),
        "security": _clamp(1.5 + 1.5 * f["auth"] + 1.0 * f["gateway"] + 0.5 * quality - 0.75 * f["errors"]),
        "reliability": _clamp(1.5 + 0.75 * min(f["replicas"], 2) + 1.0 * f["queue"] + 1.0 * quality
                              + 0.5 * f["obs"] - 0.5 * f["errors"]),
        "operability": _clamp(1.5 + 2.0 * quality + 1.0 * f["obs"] + 0.75 * managed_share - 0.08 * max(0, n - 8)),
        "delivery_speed": _clamp(5 - max(0, n - 3) * 0.3 + 0.75 * managed_share),
    }


def evaluate_heuristic(requirements: ProjectState, alternatives: dict[str, ProjectState]) -> dict[str, Any]:
    branches = list(alternatives)
    scores = {b: heuristic_scores(s) for b, s in alternatives.items()}
    out = _finish("heuristic", branches, scores, None, "", criterion_weights(requirements))
    w = criterion_weights(requirements)
    emphasis = [CRITERIA_LABELS[c].lower() for c, v in w.items() if v > 1]
    out["rationale"] = (
        f"Offline estimate from each draft's structure (component counts, caches, queues, auth, replicas, "
        f"observability and the architecture health score). '{out['recommended']}' has the best weighted average "
        f"({out['totals'][out['recommended']]} / 5)"
        + (f", with extra weight on {', '.join(emphasis)} from the NFRs" if emphasis else "") + ". "
        + _strengths_text(out) + " Configure JEV_API_KEY or an LLM key for a requirement-aware evaluation.")
    return out


def evaluate(requirements: ProjectState, alternatives: dict[str, ProjectState],
             org_id: str | None = None) -> dict[str, Any]:
    """Run the engine chain jev → llm → heuristic. ``alternatives`` maps branch name → state (2–4)."""
    if not 2 <= len(alternatives) <= 4:
        raise ValueError("compare between 2 and 4 alternatives")
    notes: list[str] = []
    if os.environ.get("JEV_API_KEY"):
        try:
            out = evaluate_jev(requirements, alternatives)
            out["notes"] = notes
            return out
        except JevError as exc:
            log.warning("Jev evaluation failed, falling back: %s", exc)
            notes.append(f"Jev unavailable: {exc}")
    else:
        notes.append("JEV_API_KEY not set")
    if llm.available():
        try:
            out = evaluate_llm(requirements, alternatives, org_id=org_id)
            out["notes"] = notes
            return out
        except (llm.LLMUnavailable, llm.LLMRefused, ValueError, KeyError, TypeError) as exc:
            log.warning("LLM evaluation failed, falling back to heuristic: %s", type(exc).__name__)
            notes.append(f"LLM evaluation failed: {type(exc).__name__}")
    else:
        notes.append("no LLM configured")
    out = evaluate_heuristic(requirements, alternatives)
    out["notes"] = notes
    return out
