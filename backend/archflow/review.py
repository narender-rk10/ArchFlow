"""AI architecture review: specialist agents fired in parallel, findings merged.

Each specialist (security, reliability, performance & scale, cost & operations) is the same versioned prompt
(``review.specialist``) with a different focus. They run at once (``agents.parallel``) under one parent trace, so
the review takes as long as the slowest specialist, and a failing specialist does not lose the others' findings.
Every finding names the components and requirements it is about and carries a ``fix``: an instruction the Edit
mode can apply as it is.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException

from . import agents, cloud_docs, conversations, llm
from .analysis import full_analysis
from .models import ProjectState

PROMPT = "review.specialist"
SEVERITIES = ("critical", "high", "medium", "low")

SPECIALISTS: dict[str, dict[str, str]] = {
    "security": {"label": "Security & privacy", "checklist": (
        "authentication and authorization on every entry point, secrets handling, encryption in transit and at "
        "rest, PII and data residency, tenant isolation, input validation at the edge, rate limiting / WAF, "
        "audit logging, prompt injection and tool permissions for AI agents")},
    "reliability": {"label": "Reliability", "checklist": (
        "single points of failure, replicas and failover, retries with backoff and timeouts, idempotency, "
        "dead-letter queues, backups and restore, graceful degradation when a dependency (including an LLM) "
        "is down, health checks")},
    "performance": {"label": "Performance & scale", "checklist": (
        "latency budget of the main flows against the NFRs, caching, synchronous chains that should be async, "
        "N+1 calls, hot partitions, horizontal scaling of stateful parts, connection pooling, backpressure")},
    "operations": {"label": "Cost & operations", "checklist": (
        "observability (logs, metrics, traces), deployment and rollback, managed vs self-hosted trade-offs, "
        "over-engineering for the stated scale, LLM token cost controls, environments, on-call burden")},
}

SCHEMA: dict[str, Any] = {"type": "object", "properties": {
    "summary": {"type": "string"},
    "findings": {"type": "array", "items": {"type": "object", "properties": {
        "severity": {"type": "string", "enum": list(SEVERITIES)},
        "title": {"type": "string"}, "detail": {"type": "string"},
        "components": {"type": "array", "items": {"type": "string"}},
        "requirement_ids": {"type": "array", "items": {"type": "string"}},
        "fix": {"type": "string"}},
        "required": ["severity", "title", "detail", "components", "fix"]}}},
    "required": ["summary", "findings"]}


def _project(state: ProjectState) -> str:
    from . import ai

    health = full_analysis(state)
    issues = [f"[{i['severity']}] {i['message']}" for i in health["architecture_issues"] if i["severity"] != "info"]
    return json.dumps({"model": json.loads(ai._state_for_prompt(state)), "health": health["health"],
                       "open_issues": issues[:20]}, indent=1)


def _clean(focus: str, data: dict[str, Any], names: set[str], req_ids: set[str]) -> list[dict[str, Any]]:
    out = []
    for f in data.get("findings") or []:
        title = " ".join(str(f.get("title") or "").split())[:160]
        if not title:
            continue
        sev = f.get("severity") if f.get("severity") in SEVERITIES else "medium"
        comps = [c for c in (f.get("components") or []) if str(c).lower() in names][:8]
        out.append({"focus": focus, "severity": sev, "title": title,
                    "detail": " ".join(str(f.get("detail") or "").split())[:1200],
                    "components": comps,
                    "requirement_ids": [r for r in (f.get("requirement_ids") or []) if r in req_ids][:8],
                    "fix": " ".join(str(f.get("fix") or "").split())[:600]})
    return out


def _key(title: str) -> str:
    return " ".join(sorted(w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 3))[:80]


def review(state: ProjectState, *, memory: str = "", org_id: str | None = None,
           focuses: list[str] | None = None) -> dict[str, Any]:
    """Run the specialists in parallel and merge their findings (most severe first, near-duplicates dropped)."""
    if not state.nodes:
        raise ValueError("There is nothing to review yet: add components first.")
    chosen = [f for f in (focuses or list(SPECIALISTS)) if f in SPECIALISTS]
    project = _project(state)
    names = {n.name.lower(): n.name for n in state.nodes.values()}

    techs = "\n".join(f"{n.name} {n.technology}" for n in state.nodes.values())

    def specialist(focus: str) -> Callable[[], dict[str, Any]]:
        spec = SPECIALISTS[focus]
        docs = cloud_docs.DocsRequest(query=f"Well-Architected {spec['label']} best practices", about=techs)
        return lambda: agents.run_json(PROMPT, SCHEMA, org_id=org_id, question=f"Review: {spec['label']}", docs=docs,
                                       variables={"focus": spec["label"], "checklist": spec["checklist"],
                                                  "project_json": project, "memory": memory})

    results = agents.parallel({f: specialist(f) for f in chosen}, name="review.panel", org_id=org_id,
                              question="Review this architecture")
    findings, report, seen = [], {}, set()
    for focus in chosen:
        r = results.get(focus)
        if isinstance(r, Exception):
            report[focus] = {"label": SPECIALISTS[focus]["label"], "status": "error",
                             "error": f"{type(r).__name__}: {str(r)[:200]}", "findings": 0, "summary": ""}
            continue
        items = _clean(focus, r, set(names), set(state.requirements))
        for item in items:
            item["components"] = [names[c.lower()] for c in item["components"]]
        report[focus] = {"label": SPECIALISTS[focus]["label"], "status": "ok", "findings": len(items),
                         "summary": str(r.get("summary") or "").strip()[:600]}
        for item in items:
            k = _key(item["title"])
            if k in seen:
                continue
            seen.add(k)
            findings.append(item)
    if all(v["status"] == "error" for v in report.values()):
        raise llm.LLMUnavailable("; ".join(v["error"] for v in report.values()))
    findings.sort(key=lambda f: SEVERITIES.index(f["severity"]))
    return {"findings": findings, "specialists": report}


def register_routes(app: FastAPI, store: Any, *, authz: Any,
                    state_or_404: Callable[[str, str], ProjectState]) -> None:
    read = authz.project("read")

    @app.post("/api/projects/{pid}/ai/review")
    async def ai_review(pid: str, branch: str = "main", a=Depends(read)):
        state = state_or_404(pid, branch)
        if not llm.available(a.org_id):
            raise HTTPException(503, "The architecture review needs an AI model: add a provider key in AI settings.")
        memory = conversations.memory_block(store, pid)
        try:
            return await asyncio.to_thread(review, state, memory=memory, org_id=a.org_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except llm.LLMUnavailable as exc:
            raise HTTPException(503, f"The AI review failed: {exc}") from exc
