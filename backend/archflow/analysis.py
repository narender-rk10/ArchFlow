"""Deterministic requirement linting, architecture validation, traceability and health.

These checks are fast, free and explainable; the AI review builds on top of them.
"""

from __future__ import annotations

import re
from typing import Any

from .models import ProjectState

AMBIGUOUS_TERMS = {
    "fast": "Define a measurable latency, e.g. 'p95 response latency < 2 s'.",
    "quick": "Define a measurable latency or duration.",
    "quickly": "Define a measurable latency or duration.",
    "scalable": "State expected load, e.g. '10,000 concurrent users'.",
    "secure": "Name the concrete controls: authentication method, encryption, audit logging.",
    "user-friendly": "Define usability criteria, e.g. 'task completed in ≤ 3 steps'.",
    "easy": "Define measurable usability criteria.",
    "robust": "Define failure modes and expected recovery behaviour.",
    "reliable": "State an availability target, e.g. '99.9 % monthly uptime'.",
    "efficient": "Define a resource or cost budget.",
    "flexible": "Name the specific variations that must be supported.",
    "real-time": "Define the maximum acceptable delay, e.g. '< 500 ms end-to-end'.",
    "high availability": "State an availability target and RTO/RPO.",
    "etc": "Replace 'etc.' with an explicit, complete list.",
    "as needed": "Specify exactly when and how.",
}

NFR_CATEGORIES = ["performance", "scalability", "availability", "security", "privacy", "observability", "cost"]

AUTH_WORDS = re.compile(r"\b(auth|login|log in|sign[ -]?in|authenticat|account|password|oauth|sso)", re.I)
PII_WORDS = re.compile(r"\b(pii|personal data|email|phone|address|gdpr|customer data)", re.I)


def lint_requirements(state: ProjectState) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for req in state.requirements.values():
        text = f"{req.title} {req.description}".lower()
        has_number = bool(re.search(r"\d", text))
        for term, suggestion in AMBIGUOUS_TERMS.items():
            if re.search(rf"\b{re.escape(term)}\b", text) and not has_number:
                issues.append(_issue("warning", "ambiguous", f"{req.id} uses the vague term '{term}'.",
                                     suggestion, requirement_ids=[req.id]))
        if not req.acceptance_criteria:
            issues.append(_issue("warning", "acceptance", f"{req.id} has no acceptance criteria.",
                                 "Add testable Given/When/Then criteria.", requirement_ids=[req.id]))
        if req.kind == "functional" and not req.actors:
            issues.append(_issue("info", "actors", f"{req.id} does not name an actor.",
                                 "Say who performs or benefits from this requirement.", requirement_ids=[req.id]))

    present = {r.category.lower() for r in state.requirements.values() if r.kind == "non_functional"}
    if state.requirements:
        for cat in NFR_CATEGORIES:
            if cat not in present:
                issues.append(_issue("info", "missing_nfr", f"No {cat} requirement defined.",
                                     f"Add a measurable {cat} non-functional requirement."))
    return issues


def validate_architecture(state: ProjectState) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    nodes, edges = state.nodes, state.edges
    connected = {e.source for e in edges.values()} | {e.target for e in edges.values()}
    incoming: dict[str, list[str]] = {}
    for e in edges.values():
        incoming.setdefault(e.target, []).append(e.source)

    for n in nodes.values():
        if n.type == "note":
            continue
        if n.id not in connected and len(nodes) > 1:
            issues.append(_issue("warning", "orphan", f"'{n.name}' is not connected to anything.",
                                 "Connect it or remove it.", node_ids=[n.id]))
        if n.type in ("database", "storage"):
            for src in incoming.get(n.id, []):
                if nodes[src].type == "client":
                    issues.append(_issue("error", "direct_db_access",
                                         f"Client '{nodes[src].name}' talks directly to '{n.name}'.",
                                         "Route data access through a service or API gateway.", node_ids=[src, n.id]))
            readers = incoming.get(n.id, [])
            if len(readers) > 1 and all(nodes[r].type == "service" for r in readers):
                issues.append(_issue("info", "shared_db", f"'{n.name}' is shared by {len(readers)} services.",
                                     "Shared databases couple services; consider ownership per service.",
                                     node_ids=[n.id, *readers]))
        if n.type == "llm" and not any(nodes[s].type in ("agent", "service", "gateway") for s in incoming.get(n.id, [])):
            issues.append(_issue("warning", "llm_exposed", f"LLM '{n.name}' has no agent/service in front of it.",
                                 "Put an agent or service between users and the model for guardrails.", node_ids=[n.id]))

    all_text = " ".join(f"{r.title} {r.description}" for r in state.requirements.values())
    has_type = {n.type for n in nodes.values()}
    names = " ".join(f"{n.name} {n.technology}" for n in nodes.values()).lower()
    if AUTH_WORDS.search(all_text) and "security" not in has_type and not re.search(r"auth|identity|cognito|keycloak|oauth", names):
        issues.append(_issue("error", "missing_auth", "Requirements mention authentication but no auth component exists.",
                             "Add an authentication/identity service."))
    if PII_WORDS.search(all_text) and not any("pii" in r.category.lower() or r.category.lower() in ("privacy", "security")
                                              for r in state.requirements.values()):
        issues.append(_issue("warning", "pii", "Requirements involve personal data but no privacy/security NFR exists.",
                             "Add PII handling, encryption and retention requirements."))
    if any(n.type == "client" for n in nodes.values()) and "gateway" not in has_type and len(nodes) > 4:
        issues.append(_issue("info", "no_gateway", "No API gateway in front of backend services.",
                             "Consider a gateway for auth, rate limiting and routing."))
    for e in edges.values():
        if e.source not in nodes or e.target not in nodes:
            issues.append(_issue("error", "dangling_edge", f"Connection {e.id} points to a missing component."))
    return issues


def traceability(state: ProjectState) -> dict[str, Any]:
    req_to_nodes: dict[str, list[str]] = {rid: [] for rid in state.requirements}
    for n in state.nodes.values():
        for rid in n.requirement_ids:
            if rid in req_to_nodes:
                req_to_nodes[rid].append(n.id)
    untraced = [rid for rid, ns in req_to_nodes.items() if not ns]
    functional = [n for n in state.nodes.values() if n.type not in ("note",)]
    unjustified = [n.id for n in functional if not n.requirement_ids]
    return {"requirement_to_nodes": req_to_nodes, "untraced_requirements": untraced, "unjustified_nodes": unjustified}


def trace_requirement(state: ProjectState, req_id: str) -> dict[str, Any]:
    """Everything affected by one requirement: linked components plus their direct connections."""
    linked = [n.id for n in state.nodes.values() if req_id in n.requirement_ids]
    related_edges = [e.id for e in state.edges.values() if e.source in linked or e.target in linked]
    neighbours = {e.source for e in state.edges.values() if e.id in related_edges} | \
                 {e.target for e in state.edges.values() if e.id in related_edges}
    return {"requirement_id": req_id, "node_ids": linked, "edge_ids": related_edges,
            "neighbour_ids": sorted(neighbours - set(linked))}


def health(state: ProjectState) -> dict[str, Any]:
    lint = lint_requirements(state)
    arch = validate_architecture(state)
    trace = traceability(state)
    reqs = list(state.requirements.values())
    n_req = len(reqs) or 1
    complete = sum(1 for r in reqs if r.acceptance_criteria and (r.actors or r.kind == "non_functional"))
    traced = len(reqs) - len(trace["untraced_requirements"])
    approved = sum(1 for r in reqs if r.status in ("approved", "implemented"))
    arch_errors = sum(1 for i in arch if i["severity"] == "error")
    arch_warn = sum(1 for i in arch if i["severity"] == "warning")
    arch_score = max(0, 100 - 20 * arch_errors - 7 * arch_warn) if state.nodes else 0
    return {
        "requirements_complete": round(100 * complete / n_req) if reqs else 0,
        "requirements_approved": round(100 * approved / n_req) if reqs else 0,
        "traceability": round(100 * traced / n_req) if reqs else 0,
        "architecture_score": arch_score,
        "counts": {"requirements": len(reqs), "components": len(state.nodes), "connections": len(state.edges)},
        "issues": {
            "errors": sum(1 for i in lint + arch if i["severity"] == "error"),
            "warnings": sum(1 for i in lint + arch if i["severity"] == "warning"),
            "info": sum(1 for i in lint + arch if i["severity"] == "info"),
        },
    }


def full_analysis(state: ProjectState) -> dict[str, Any]:
    return {
        "requirement_issues": lint_requirements(state),
        "architecture_issues": validate_architecture(state),
        "traceability": traceability(state),
        "health": health(state),
    }


def _issue(severity: str, code: str, message: str, suggestion: str = "",
           node_ids: list[str] | None = None, requirement_ids: list[str] | None = None) -> dict[str, Any]:
    return {"severity": severity, "code": code, "message": message, "suggestion": suggestion,
            "node_ids": node_ids or [], "requirement_ids": requirement_ids or []}
