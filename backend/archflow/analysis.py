"""Deterministic requirement linting, architecture validation, traceability and health.

These checks are fast, free and explainable; the AI review builds on top of them.
"""

from __future__ import annotations

import re
from typing import Any

from .models import LLD_TYPES, ProjectState

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
# What a non-functional requirement covers is judged from its category AND its wording, so e.g. a "Compliance"
# requirement about GDPR and PII encryption counts for privacy and security.
NFR_SIGNALS = {
    "performance": r"performance|latency|response time|\bp9[059]\b|throughput|\d\s*ms\b",
    "scalability": r"scalab|\bpeak|concurrent|\bload\b|orders/day|requests per|\brps\b|elastic|autoscal",
    "availability": r"availab|uptime|\bsla\b|99\.\d|failover|disaster|\brecovery\b|\brto\b|\brpo\b",
    "security": r"secur|authenticat|authori[sz]|encrypt|\btls\b|owasp|\brbac\b|least privilege|\bsecrets?\b",
    "privacy": r"privacy|gdpr|\bpii\b|personal data|data subject|consent|right to be forgotten|retention|\bccpa\b",
    "observability": r"observab|monitor|\blogging\b|tracing|metrics|alerting",
    "cost": r"\bcosts?\b|budget|\bspend|\$|€",
}


def nfr_coverage(state: ProjectState) -> set[str]:
    covered: set[str] = set()
    for r in state.requirements.values():
        if r.kind != "non_functional":
            continue
        text = f"{r.category} {r.title} {r.description} {' '.join(r.acceptance_criteria)}".lower()
        covered |= {cat for cat, pat in NFR_SIGNALS.items() if cat in r.category.lower() or re.search(pat, text)}
    return covered

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

    present = nfr_coverage(state)
    if state.requirements:
        for cat in NFR_CATEGORIES:
            if cat not in present:
                issues.append(_issue("info", "missing_nfr", f"No {cat} requirement defined.",
                                     f"Add a measurable {cat} non-functional requirement."))
    return issues


def diagram_of(state: ProjectState, node) -> str:
    """The diagram a node is drawn on: its parent's id, or "" (top level) when it has none or the parent is gone."""
    return node.parent_id if node.parent_id and node.parent_id in state.nodes else ""


# LLD elements that legitimately stand alone in a diagram (e.g. a lookup table without foreign keys).
_MAY_BE_UNCONNECTED = {"note", "table"}


def validate_architecture(state: ProjectState) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    all_nodes, edges = state.nodes, state.edges
    # System-level checks (data access, gateways, auth…) only look at the top-level (HLD) diagram.
    nodes = {nid: n for nid, n in all_nodes.items() if not diagram_of(state, n)}
    connected = {e.source for e in edges.values()} | {e.target for e in edges.values()}
    incoming: dict[str, list[str]] = {}
    for e in edges.values():
        incoming.setdefault(e.target, []).append(e.source)
    per_diagram: dict[str, int] = {}
    for n in all_nodes.values():
        if n.type != "note":
            per_diagram[diagram_of(state, n)] = per_diagram.get(diagram_of(state, n), 0) + 1

    # Orphans are judged per diagram: an element is compared only with the others on its own level.
    for n in all_nodes.values():
        if n.type in _MAY_BE_UNCONNECTED or n.id in connected:
            continue
        level = diagram_of(state, n)
        if per_diagram.get(level, 0) > 1:
            where = f" in the internals of '{all_nodes[level].name}'" if level else ""
            issues.append(_issue("warning" if not level else "info", "orphan",
                                 f"'{n.name}' is not connected to anything{where}.",
                                 "Connect it or remove it.", node_ids=[n.id]))

    for n in nodes.values():
        if n.type == "note":
            continue
        if n.type in ("database", "storage"):
            for src in incoming.get(n.id, []):
                if src in nodes and nodes[src].type == "client":
                    issues.append(_issue("error", "direct_db_access",
                                         f"Client '{nodes[src].name}' talks directly to '{n.name}'.",
                                         "Route data access through a service or API gateway.", node_ids=[src, n.id]))
            # Backends sharing one store couple their schemas and deployments. Count every backend kind (an agent
            # or MCP server reading the store counts too), and warn once three or more share it.
            readers = [r for r in dict.fromkeys(incoming.get(n.id, [])) if r in nodes
                       and nodes[r].type in ("service", "agent", "mcp", "component")]
            if len(readers) > 1:
                issues.append(_issue("warning" if len(readers) >= 3 else "info", "shared_db",
                                     f"'{n.name}' is shared by {len(readers)} services.",
                                     "Shared databases couple services; give each service ownership of its data "
                                     "(its own schema or database) and share through APIs or events.",
                                     node_ids=[n.id, *readers]))
        if n.type == "llm" and not any(nodes[s].type in ("agent", "service", "gateway", "security", "mcp")
                                       for s in incoming.get(n.id, []) if s in nodes):
            issues.append(_issue("warning", "llm_exposed", f"LLM '{n.name}' has no agent, service or guardrail in front of it.",
                                 "Put an agent or service between users and the model for guardrails.", node_ids=[n.id]))

    all_text = " ".join(f"{r.title} {r.description}" for r in state.requirements.values())
    has_type = {n.type for n in nodes.values()}
    names = " ".join(f"{n.name} {n.technology}" for n in nodes.values()).lower()
    if AUTH_WORDS.search(all_text) and "security" not in has_type and not re.search(r"auth|identity|cognito|keycloak|oauth", names):
        issues.append(_issue("error", "missing_auth", "Requirements mention authentication but no auth component exists.",
                             "Add an authentication/identity service."))
    if PII_WORDS.search(all_text) and not ({"privacy", "security"} & nfr_coverage(state)):
        issues.append(_issue("warning", "pii", "Requirements involve personal data but no privacy/security NFR exists.",
                             "Add PII handling, encryption and retention requirements."))
    if any(n.type == "client" for n in nodes.values()) and "gateway" not in has_type and len(nodes) > 4:
        issues.append(_issue("info", "no_gateway", "No API gateway in front of backend services.",
                             "Consider a gateway for auth, rate limiting and routing."))
    for e in edges.values():
        if e.source not in all_nodes or e.target not in all_nodes:
            issues.append(_issue("error", "dangling_edge", f"Connection {e.id} points to a missing component."))
    return issues


def traceability(state: ProjectState) -> dict[str, Any]:
    req_to_nodes: dict[str, list[str]] = {rid: [] for rid in state.requirements}
    for n in state.nodes.values():
        for rid in n.requirement_ids:
            if rid in req_to_nodes:
                req_to_nodes[rid].append(n.id)
    untraced = [rid for rid, ns in req_to_nodes.items() if not ns]
    # LLD elements inherit their justification from the HLD component they belong to.
    functional = [n for n in state.nodes.values() if n.type not in ("note",) and not diagram_of(state, n)
                  and n.type not in LLD_TYPES]
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
        "counts": {"requirements": len(reqs),
                   "components": sum(1 for n in state.nodes.values() if not diagram_of(state, n)),
                   "lld_elements": sum(1 for n in state.nodes.values() if diagram_of(state, n)),
                   "connections": len(state.edges)},
        "issues": {
            "errors": sum(1 for i in lint + arch if i["severity"] == "error"),
            "warnings": sum(1 for i in lint + arch if i["severity"] == "warning"),
            "info": sum(1 for i in lint + arch if i["severity"] == "info"),
        },
    }


def full_analysis(state: ProjectState, code_state: ProjectState | None = None) -> dict[str, Any]:
    """``code_state``: the architecture reconstructed from the project's latest code graph, if any; adds
    ``drift_issues`` and ``health.code_alignment`` (see codegraph/drift.py)."""
    out = {
        "requirement_issues": lint_requirements(state),
        "architecture_issues": validate_architecture(state),
        "traceability": traceability(state),
        "health": health(state),
    }
    if code_state is not None:
        from .codegraph.drift import drift

        d = drift(state, code_state)
        out["drift_issues"] = d["issues"]
        out["health"]["code_alignment"] = d["alignment"]
    return out


def _issue(severity: str, code: str, message: str, suggestion: str = "",
           node_ids: list[str] | None = None, requirement_ids: list[str] | None = None) -> dict[str, Any]:
    return {"severity": severity, "code": code, "message": message, "suggestion": suggestion,
            "node_ids": node_ids or [], "requirement_ids": requirement_ids or []}
