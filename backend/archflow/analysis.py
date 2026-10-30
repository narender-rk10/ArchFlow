"""Deterministic requirement linting, architecture validation, traceability, stale links and health.

These checks are fast, free and explainable; the AI review builds on top of them. Soft-deleted requirements
are left out of every check (``full_analysis`` works on ``models.without_deleted``).
"""

from __future__ import annotations

import re
from typing import Any

from .models import LLD_TYPES, ProjectState, without_deleted
from .ops import REVIEWED

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
    state = without_deleted(state)
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


def validate_architecture(state: ProjectState, context: str = "") -> list[dict[str, Any]]:
    """``context``: extra text (e.g. the product idea) used, with the requirements, to detect the domain."""
    state = without_deleted(state)
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
    issues += flow_checks(state, context)
    issues += completeness_issues(state, context)
    return issues


# --------------------------------------------------------------------------- flow checks
# Roles are recognised from a component's type plus its name/technology (never its free-text description, which
# often mentions neighbours: "pushes updates to the WebSocket gateway").
_AUTH_RE = re.compile(r"auth|identity|\bidp\b|cognito|keycloak|oauth|oidc|entra|okta|\bsso\b|login|sign[- ]?in|clerk|"
                      r"supertokens|\bory\b|zitadel|fusionauth", re.I)
_NOT_IDENTITY_RE = re.compile(r"\bkms\b|key management|key vault|\bvault\b|secret|\bhsm\b|\bwaf\b|firewall|guard|shield|"
                              r"ddos|scanner|siem|audit|fraud", re.I)
_KMS_RE = re.compile(r"\bkms\b|key management|key vault|\bvault\b|secrets? manager|secret manager|\bhsm\b", re.I)
REALTIME_RE = re.compile(r"web ?sockets?|\bwss?\b|mqtt|socket\.io|\bsse\b|server[- ]sent|signalr|iot core|iot hub|"
                         r"pusher|\bably\b|centrifugo|appsync|web pubsub|real[- ]?time (gateway|channel|hub|server|service)|"
                         r"push (gateway|channel|server)|live[- ]?(updates?|tracking|location) (gateway|hub|service|server)", re.I)
_STATIC_EDGE_RE = re.compile(r"\bcdn\b|cloudfront|front door|cloud cdn|cloudflare|akamai|fastly|load balanc|\balb\b|"
                             r"\bnlb\b|\belb\b|\bdns\b|route ?53|static (site|assets|hosting)", re.I)
_CONSUME_RE = re.compile(r"consum|subscri|listen|\bpoll|\bpull|\bsub\b|receive|fan[- ]?out|trigger", re.I)
_READ_WORDS = re.compile(r"\b(reads?|query|queries|select|lookup|search|fetch|get|replica|reports?|dashboards?)\b", re.I)
_WRITE_WORDS = re.compile(r"\b(writes?|insert|ingest|upload|put|archive|backup|sink|persist|append|store|save|updates?)\b", re.I)


def _read_only(text: str) -> bool:
    return bool(_READ_WORDS.search(text)) and not _WRITE_WORDS.search(text)


def _write_only(text: str) -> bool:
    return bool(_WRITE_WORDS.search(text)) and not _READ_WORDS.search(text)
_SCHEDULED_RE = re.compile(r"cron|schedul|\bbatch|\bjobs?\b|\betl\b|timer|nightly|periodic", re.I)
_SENSITIVE_RE = re.compile(r"encrypt|\bpii\b|gdpr|personal data|hipaa|\bpci\b|sensitive data|data protection|"
                           r"special category|health data", re.I)
_AVAIL_RE = re.compile(r"99\.9|high availability|multi[- ]?az|failover|\bha\b|uptime|\bsla\b|\brto\b|\brpo\b", re.I)
_REPLICATED_RE = re.compile(r"multi[- ]?az|replica|replicat|cluster|\bha\b|high availability|aurora|dynamodb|cosmos|spanner|"
                            r"firestore|bigtable|cockroach|cassandra|alloydb|atlas|standby|failover|geo[- ]redundant|"
                            r"zone[- ]redundant|serverless|managed", re.I)
_TRAFFIC_PEAK_RE = re.compile(r"(traffic|load|usage|lunch|dinner|seasonal|black friday)\w* (peaks?|spikes?|bursts?|surges?)|"
                              r"peak (load|traffic|hours?|times?|demand)|\bbursty\b|flash sales?", re.I)
_ERASURE_RE = re.compile(r"right to (be forgotten|erasure)|erasure|data deletion|delete (their|personal|customer|user) data|"
                         r"deleted on request|deletion request", re.I)
_EVENT_LOG_RE = re.compile(r"kafka|kinesis|event hubs|\bmsk\b|pulsar|event ?store|redpanda|event (log|stream)|eventbridge", re.I)


def _role_text(n) -> str:
    return f"{n.name} {n.technology}"


def is_auth(n) -> bool:
    """An identity/authentication component (not a key vault, WAF or guardrail)."""
    text = _role_text(n)
    if n.type == "security":
        return not _NOT_IDENTITY_RE.search(text) or bool(_AUTH_RE.search(text) and not _KMS_RE.search(text))
    return n.type in ("service", "external") and bool(_AUTH_RE.search(text)) and not _NOT_IDENTITY_RE.search(text)


def is_key_management(n) -> bool:
    if _KMS_RE.search(_role_text(n)):
        return True
    from . import catalog

    entry = catalog.match({"technology": n.technology, "name": n.name, "catalog_id": n.catalog_id})
    return bool(entry and entry["kind"] == "security.secrets")


def is_realtime(n) -> bool:
    return n.type in ("gateway", "service", "external") and bool(REALTIME_RE.search(_role_text(n)))


def _is_static_edge(n) -> bool:
    return n.type == "gateway" and bool(_STATIC_EDGE_RE.search(_role_text(n))) and not REALTIME_RE.search(_role_text(n))


def _edge_text(e) -> str:
    return f"{e.protocol} {e.label}"


def _top_level_graph(state: ProjectState):
    nodes = {nid: n for nid, n in state.nodes.items() if not diagram_of(state, n) and n.type != "note"}
    edges = [e for e in state.edges.values() if e.source in nodes and e.target in nodes and e.source != e.target]
    out: dict[str, list] = {nid: [] for nid in nodes}
    inc: dict[str, list] = {nid: [] for nid in nodes}
    for e in edges:
        out[e.source].append(e)
        inc[e.target].append(e)
    return nodes, edges, out, inc


def realtime_status(state: ProjectState) -> dict[str, str]:
    """node id → "ok" | "one_way" (only sends into the backend) | "detached" (no backend link), for every realtime
    channel that clients use."""
    nodes, _, out, inc = _top_level_graph(state)
    status: dict[str, str] = {}
    for nid, n in nodes.items():
        if not is_realtime(n):
            continue
        neighbours = [e.source for e in inc[nid]] + [e.target for e in out[nid]]
        if not any(nodes[x].type == "client" for x in neighbours):
            continue
        backend = lambda x: nodes[x].type not in ("client", "security")  # noqa: E731
        inbound = any(backend(e.source) for e in inc[nid])
        subscribes = any(backend(e.target) and _CONSUME_RE.search(_edge_text(e)) for e in out[nid])
        if inbound or subscribes:
            status[nid] = "ok"
        elif any(backend(e.target) for e in out[nid]):
            status[nid] = "one_way"
        else:
            status[nid] = "detached"
    return status


def flow_checks(state: ProjectState, context: str = "") -> list[dict[str, Any]]:
    """Deterministic data-flow checks on the top-level diagram: entry-point authentication, producer/consumer
    completeness of queues, realtime return paths, data-store writers/readers, reachability, and checks driven by
    the non-functional requirements (key management, availability, scale buffers, erasure vs. event logs)."""
    nodes, edges, out, inc = _top_level_graph(state)
    issues: list[dict[str, Any]] = []
    if not nodes:
        return issues
    auth_ids = {nid for nid, n in nodes.items() if is_auth(n)}
    callers = {nid for nid, n in nodes.items() if n.type in ("client", "external")}

    # 1. Entry points must authenticate (directly, through a session/proxy service, or behind an authenticated gateway).
    def adjacent_auth(nid: str) -> bool:
        return any(e.target in auth_ids for e in out[nid]) or any(e.source in auth_ids for e in inc[nid])

    def authed(nid: str, seen: frozenset = frozenset()) -> bool:
        n = nodes[nid]
        if _is_static_edge(n) or adjacent_auth(nid):
            return True
        if nid in seen:
            return False
        seen = seen | {nid}
        for e in out[nid]:  # E → session service → auth
            t = nodes[e.target]
            if t.type in ("service", "agent") and adjacent_auth(e.target):
                return True
            if t.type == "gateway" and authed(e.target, seen):  # load balancer → authenticated API gateway
                return True
        # reached only through gateways: inherits their authentication
        ups = [e.source for e in inc[nid]]
        if ups and not any(u in callers for u in ups):
            gws = [u for u in ups if nodes[u].type == "gateway"]
            if gws and all(authed(u, seen) for u in gws):
                return True
        return False

    entries = []
    for nid, n in nodes.items():
        from_callers = any(e.source in callers for e in inc[nid]) or (
            is_realtime(n) and any(nodes[e.target].type == "client" for e in out[nid]))
        if n.type == "gateway" and (from_callers or not inc[nid]):
            entries.append(nid)
        elif n.type in ("service", "agent", "mcp") and any(nodes[e.source].type == "client" for e in inc[nid]):
            entries.append(nid)
    if entries and callers:
        if auth_ids:
            for nid in entries:
                if not authed(nid):
                    n = nodes[nid]
                    public = any(e.source in callers for e in inc[nid]) or is_realtime(n)
                    issues.append(_issue("error" if public else "warning", "unauthenticated_entry",
                                         f"Entry point '{n.name}' has no path to an identity/auth component.",
                                         f"Connect '{n.name}' to the auth service (token validation on connect/request) "
                                         "or put it behind the authenticated API gateway.", node_ids=[nid]))
        elif not AUTH_WORDS.search(" ".join(f"{r.title} {r.description}" for r in state.requirements.values())):
            # (when requirements mention authentication, validate_architecture already reports missing_auth)
            issues.append(_issue("warning", "unauthenticated_entry",
                                 "No identity/auth component: entry points "
                                 + ", ".join(f"'{nodes[x].name}'" for x in entries[:4]) + " are unauthenticated.",
                                 "Add an identity provider and validate tokens at every entry point.", node_ids=entries[:4]))

    # 2. Queues need producers and consumers.
    for nid, n in nodes.items():
        if n.type != "queue" or not (inc[nid] or out[nid]):
            continue
        producers = [e.source for e in inc[nid] if not _CONSUME_RE.search(_edge_text(e))]
        consumers = [e.target for e in out[nid]] + [e.source for e in inc[nid] if _CONSUME_RE.search(_edge_text(e))]
        if producers and not consumers:
            issues.append(_issue("warning", "queue_no_consumer", f"Messages sent to '{n.name}' are never consumed.",
                                 f"Connect '{n.name}' to the service(s) that process its messages (queue → consumer).",
                                 node_ids=[nid]))
        elif consumers and not producers:
            issues.append(_issue("warning", "queue_no_producer", f"Nothing publishes to '{n.name}', but services consume it.",
                                 f"Connect the producing service(s) to '{n.name}' (producer → queue).", node_ids=[nid]))

    # 3. Realtime channels clients depend on need a return path from the backend.
    for nid, st in realtime_status(state).items():
        n = nodes[nid]
        if st == "one_way":
            sinks = ", ".join(f"'{nodes[e.target].name}'" for e in out[nid] if nodes[e.target].type not in ("client", "security"))
            issues.append(_issue("error", "realtime_return_path",
                                 f"Realtime return path missing: '{n.name}' only sends data into {sinks}; nothing flows "
                                 "back to it, so clients never receive live updates.",
                                 f"Add a path from the backend to '{n.name}' (e.g. event bus → '{n.name}' subscription, "
                                 "or the tracking service pushes to it).", node_ids=[nid]))
        elif st == "detached":
            issues.append(_issue("warning", "realtime_return_path",
                                 f"Realtime channel '{n.name}' is not connected to any backend component.",
                                 f"Connect '{n.name}' to the service or event bus that produces the updates it pushes.",
                                 node_ids=[nid]))

    # 4. Data stores need a writer and a reader.
    for nid, n in nodes.items():
        if n.type not in ("database", "storage") or not (inc[nid] or out[nid]):
            continue
        # An unlabelled "service → store" connection means read/write access.
        writers = [e for e in inc[nid] if not _read_only(_edge_text(e))]
        readers = [e for e in inc[nid] if not _write_only(_edge_text(e))] + out[nid]
        if not writers:
            issues.append(_issue("warning", "datastore_no_writer", f"Nothing writes to '{n.name}'.",
                                 f"Connect the component that owns and writes '{n.name}'.", node_ids=[nid]))
        elif not readers:
            issues.append(_issue("info", "datastore_no_reader", f"'{n.name}' is written but never read.",
                                 "Connect its readers, or confirm it is an archive.", node_ids=[nid]))

    # 5. Services must be reachable from a client, gateway, queue, external caller or schedule.
    sources = [nid for nid, n in nodes.items()
               if n.type in ("client", "external", "gateway", "queue") or _SCHEDULED_RE.search(_role_text(n))]
    flow_out: dict[str, set[str]] = {nid: set() for nid in nodes}
    for e in edges:
        if _CONSUME_RE.search(_edge_text(e)) and nodes[e.target].type in ("queue", "cache", "database", "storage"):
            flow_out[e.target].add(e.source)  # "worker → queue (consume)": data flows queue → worker
        else:
            flow_out[e.source].add(e.target)
    reached, stack = set(sources), list(sources)
    while stack:
        for nxt in flow_out[stack.pop()]:
            if nxt not in reached:
                reached.add(nxt)
                stack.append(nxt)
    for nid, n in nodes.items():
        if n.type in ("service", "agent") and nid not in reached and (inc[nid] or out[nid]):
            issues.append(_issue("warning", "unreachable",
                                 f"'{n.name}' has no inbound path from any client, gateway, queue or schedule.",
                                 "Connect what triggers it (gateway route, queue subscription or schedule); "
                                 "if it consumes a queue, draw queue → service.", node_ids=[nid]))

    # 6. Requirement-driven checks.
    nfr = " ".join(f"{r.category} {r.title} {r.description} {' '.join(r.acceptance_criteria)}"
                   for r in state.requirements.values() if r.kind == "non_functional")
    all_req = " ".join(f"{r.title} {r.description} {' '.join(r.acceptance_criteria)}" for r in state.requirements.values())
    kms = [nid for nid, n in nodes.items() if is_key_management(n)]
    if _SENSITIVE_RE.search(nfr) and not kms:
        issues.append(_issue("warning", "missing_key_management",
                             "Requirements call for encryption of personal/sensitive data but no key-management component exists.",
                             "Add a KMS / key vault (e.g. AWS KMS, Azure Key Vault, HashiCorp Vault) used by the data stores "
                             "and services."))
    if _AVAIL_RE.search(nfr):
        single = [nid for nid, n in nodes.items() if n.type == "database"
                  and not _REPLICATED_RE.search(f"{n.name} {n.technology} {n.description} {' '.join(n.props.values())}")]
        if single:
            issues.append(_issue("info", "single_instance_datastore",
                                 "An availability target is set but " + ", ".join(f"'{nodes[x].name}'" for x in single[:4])
                                 + " show no replication or failover.",
                                 "Use multi-AZ / replicated deployments (state it in the technology or description).",
                                 node_ids=single[:4]))
    if (_large_scale(nfr) or _TRAFFIC_PEAK_RE.search(nfr)) and not any(n.type in ("cache", "queue") for n in nodes.values()):
        issues.append(_issue("warning", "no_buffer_for_scale",
                             "Scale/peak requirements exist but there is no cache or queue to absorb load.",
                             "Add a cache for hot reads and a queue for asynchronous work."))
    queues = [nid for nid, n in nodes.items() if n.type == "queue" and _EVENT_LOG_RE.search(_role_text(n))]
    if queues and _ERASURE_RE.search(all_req):
        issues.append(_issue("info", "erasure_event_log",
                             "Right-to-erasure is required, but " + ", ".join(f"'{nodes[x].name}'" for x in queues[:3])
                             + " retains events immutably.",
                             "Keep PII out of event payloads (opaque ids) or encrypt it per subject and delete the key "
                             "(crypto-shredding); set retention limits.", node_ids=queues[:3]))
    return issues


_MAGNITUDE_RE = re.compile(r"(\d[\d,.]*)\s*(k|m|million|thousand)?\b", re.I)


def _large_scale(text: str) -> bool:
    """True when the text names a volume of at least 10,000 (users, orders/day, requests…)."""
    for num, unit in _MAGNITUDE_RE.findall(text or ""):
        try:
            value = float(num.replace(",", "")) if num.count(".") <= 1 else 0
        except ValueError:
            continue
        unit = unit.lower()
        value *= 1_000 if unit in ("k", "thousand") else 1_000_000 if unit in ("m", "million") else 1
        if value >= 10_000 and not re.fullmatch(r"99\.?\d*", num):
            return True
    return False


def flow_facts(state: ProjectState) -> dict[str, bool]:
    """Flow conditions the checklists refer to. ``realtime_return``: clients have a realtime channel (whether its
    return path is complete is reported separately by the ``realtime_return_path`` flow check, not twice)."""
    return {"realtime_return": bool(realtime_status(state))}


def completeness_issues(state: ProjectState, context: str = "") -> list[dict[str, Any]]:
    """Capabilities the detected domain(s) expect but the top-level architecture lacks (see checklists.py)."""
    from . import checklists

    text = context + " " + " ".join(f"{r.title} {r.description} {' '.join(r.acceptance_criteria)}"
                                    for r in state.requirements.values())
    nodes = [n for n in state.nodes.values() if not diagram_of(state, n) and n.type != "note"]
    if not nodes:
        return []
    out = []
    for cap in checklists.missing_capabilities(text, nodes, flow_facts(state)):
        out.append(_issue(cap["severity"], "completeness",
                          f"Missing capability ({', '.join(cap['domains'])}): {cap['label']}.",
                          cap["why"], capability=cap["id"]))
    return out


def traceability(state: ProjectState) -> dict[str, Any]:
    state = without_deleted(state)
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


# What a requirement says. A change to these after a linked component last changed makes the link stale; status,
# provenance and deletion do not.
REQUIREMENT_CONTENT = ("title", "description", "kind", "category", "priority", "actors", "acceptance_criteria")


def stale_links(state: ProjectState) -> list[dict[str, Any]]:
    """Component → requirement links where the requirement changed after the component last did (or was last
    reviewed against it, see ``ops`` ``mark_reviewed``). Computed from the revision bookkeeping, nothing stored."""
    out: list[dict[str, Any]] = []
    for n in state.nodes.values():
        changed = max((r for f, r in n.field_revs.items() if f != "position" and not f.startswith(REVIEWED)), default=0)
        for rid in n.requirement_ids:
            req = state.requirements.get(rid)
            if not req or req.deleted:
                continue
            seen = max(changed, n.field_revs.get(REVIEWED + rid, 0))
            fields = [f for f in REQUIREMENT_CONTENT if req.field_revs.get(f, 0) > seen]
            if fields:
                out.append({"node_id": n.id, "requirement_id": rid, "fields": fields, "component_rev": seen,
                            "requirement_rev": max(req.field_revs[f] for f in fields)})
    return out


def health(state: ProjectState) -> dict[str, Any]:
    state = without_deleted(state)
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
    stale = stale_links(state)
    state = without_deleted(state)
    out = {
        "requirement_issues": lint_requirements(state),
        "architecture_issues": validate_architecture(state),
        "traceability": traceability(state),
        "health": health(state),
        "stale_links": stale,
    }
    if code_state is not None:
        from .codegraph.drift import drift

        d = drift(state, code_state)
        out["drift_issues"] = d["issues"]
        out["health"]["code_alignment"] = d["alignment"]
    return out


def _issue(severity: str, code: str, message: str, suggestion: str = "",
           node_ids: list[str] | None = None, requirement_ids: list[str] | None = None,
           capability: str = "") -> dict[str, Any]:
    out = {"severity": severity, "code": code, "message": message, "suggestion": suggestion,
           "node_ids": node_ids or [], "requirement_ids": requirement_ids or []}
    if capability:
        out["capability"] = capability
    return out
