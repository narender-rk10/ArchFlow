"""Deterministic review of proposed architecture options (design-chat cards).

An *option* is the card shape the design chat proposes: ``{"components": [{key, name, type, technology,
description, layer}], "connections": [{from, to, protocol}], ...}``. This module

* normalizes technologies — exactly one decided technology per component ("Go / Node.js on EKS" → "Go on EKS",
  alternatives recorded in the description);
* checks an option by applying it to a temporary project (``chat.architecture_ops`` + ``apply_ops``) and running
  ``analysis.validate_architecture`` (structure, data-flow and domain-completeness checks);
* repairs what can be repaired without a model (``auto_fix``): connect entry points to auth, add the realtime
  return path, add key management / cache / checklist capabilities with sensible wiring;
* adds requirement stubs for uncovered functional areas and missing NFRs.

The LLM review-and-repair loop in ``chat.py`` builds on these functions; offline, they are the whole review.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from . import checklists
from .analysis import (REALTIME_RE, _STATIC_EDGE_RE, is_auth, is_key_management, nfr_coverage,
                       validate_architecture)
from .models import ProjectState
from .ops import apply_ops, slugify

SEVERITY_WEIGHT = {"error": 20, "warning": 7, "info": 1}
CLOUD_AUTH = {"aws": "Amazon Cognito", "azure": "Microsoft Entra ID", "gcp": "Identity Platform", "agnostic": "Keycloak"}
CLOUD_CACHE = {"aws": "Amazon ElastiCache (Redis)", "azure": "Azure Cache for Redis", "gcp": "Memorystore Redis",
               "agnostic": "Redis"}

# --------------------------------------------------------------------------- one technology per component
_ALT_SPLIT = re.compile(r"\s+/\s+|\s+or\s+|\s*\|\s*", re.I)
_PAREN_ALT = re.compile(r"\s*\((?:or|e\.g\.?|alternatively|alt\.?)\s+([^)]*)\)", re.I)
_SUFFIX = re.compile(r"\s+(?:on|via|in|with)\s+\S.*$", re.I)


def normalize_technology(tech: str) -> tuple[str, list[str]]:
    """("Go / Node.js on EKS") → ("Go on EKS", ["Node.js"]). Slashes without spaces (TCP/IP) are kept."""
    tech = (tech or "").strip()
    alternatives: list[str] = []
    m = _PAREN_ALT.search(tech)
    if m:
        alternatives += [p.strip() for p in re.split(r",|\s+or\s+|\s+/\s+", m.group(1)) if p.strip()]
        tech = (tech[:m.start()] + tech[m.end():]).strip()
    parts = [p.strip() for p in _ALT_SPLIT.split(tech) if p and p.strip()]
    if len(parts) < 2:
        return tech, alternatives
    first, rest = parts[0], parts[1:]
    suffix = _SUFFIX.search(rest[-1])
    if suffix and not _SUFFIX.search(first):  # the deployment target usually applies to every alternative
        first += suffix.group(0)
        rest[-1] = rest[-1][:suffix.start()].strip()
    return first, [r for r in rest if r] + alternatives


def normalize_option(option: dict[str, Any]) -> list[str]:
    """Pick one technology per component in place; returns a note per change."""
    notes = []
    for c in option.get("components") or []:
        tech, alts = normalize_technology(c.get("technology", ""))
        if alts:
            notes.append(f"'{c.get('name')}': decided on {tech} (alternatives: {', '.join(alts)})")
            c["technology"] = tech
            extra = f"Alternatives considered: {', '.join(alts)}."
            if extra not in (c.get("description") or ""):
                c["description"] = f"{c.get('description', '').rstrip()} {extra}".strip()
    return notes


# --------------------------------------------------------------------------- checking an option
def option_state(option: dict[str, Any], requirements: list[dict[str, Any]]) -> tuple[ProjectState, dict[str, str]]:
    """The option applied to an empty project, plus node id → component key."""
    from .chat import architecture_ops  # local: chat imports this module

    key_to_id: dict[str, str] = {}
    ops = architecture_ops(ProjectState(), option, requirements, ids_out=key_to_id)
    state, _ = apply_ops(ProjectState(), ops)
    return state, {nid: key for key, nid in key_to_id.items()}


def check_option(option: dict[str, Any], requirements: list[dict[str, Any]], context: str = "") -> list[dict[str, Any]]:
    """Architecture issues (incl. flow and completeness) of an option; node_ids are mapped back to component keys."""
    try:
        state, id_to_key = option_state(option, requirements)
    except Exception as exc:  # malformed option: report it instead of failing the whole card
        return [{"severity": "error", "code": "invalid_option", "message": f"Option could not be applied: {exc}",
                 "suggestion": "", "node_ids": [], "requirement_ids": []}]
    issues = validate_architecture(state, context)
    for i in issues:
        i["node_ids"] = [id_to_key.get(n, n) for n in i["node_ids"]]
    return issues


def blocking(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [i for i in issues if i["severity"] in ("error", "warning")]


def weight(issues: list[dict[str, Any]]) -> int:
    return sum(SEVERITY_WEIGHT.get(i["severity"], 0) for i in issues)


def brief(issues: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [{"severity": i["severity"], "code": i["code"], "message": i["message"]} for i in blocking(issues)]


# --------------------------------------------------------------------------- deterministic repair
def infer_cloud(option: dict[str, Any]) -> str:
    text = " ".join(f"{c.get('technology', '')} {c.get('name', '')}" for c in option.get("components") or []).lower()
    scores = {"aws": len(re.findall(r"\baws\b|amazon|lambda|dynamodb|cognito|\bsqs\b|\bmsk\b|fargate|\beks\b|\becs\b", text)),
              "azure": len(re.findall(r"azure|entra|cosmos|\baks\b", text)),
              "gcp": len(re.findall(r"google|\bgcp\b|cloud run|bigquery|firestore|vertex|\bgke\b|pub/sub|spanner", text))}
    best = max(scores, key=scores.get)
    return best if scores[best] else "agnostic"


class _Opt:
    """Small helper for editing an option's components and connections."""

    def __init__(self, option: dict[str, Any]):
        self.o = option
        self.o.setdefault("components", [])
        self.o.setdefault("connections", [])

    @property
    def comps(self) -> list[dict[str, Any]]:
        return self.o["components"]

    def get(self, key: str) -> dict[str, Any] | None:
        return next((c for c in self.comps if c["key"] == key), None)

    def ns(self, c: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(type=c.get("type", "service"), name=c.get("name", ""), technology=c.get("technology", ""),
                               catalog_id=c.get("catalog_id", ""))

    def of_type(self, *types: str) -> list[dict[str, Any]]:
        return [c for c in self.comps if c.get("type") in types]

    def connected(self, a: str, b: str) -> bool:
        return any(e["from"] == a and e["to"] == b for e in self.o["connections"])

    def link(self, a: str, b: str, protocol: str = "") -> bool:
        if a == b or self.connected(a, b) or not self.get(a) or not self.get(b):
            return False
        self.o["connections"].append({"from": a, "to": b, "protocol": protocol})
        return True

    def degree(self, key: str) -> int:
        return sum(1 for e in self.o["connections"] if key in (e["from"], e["to"]))

    def new_key(self, base: str) -> str:
        key, i = slugify(base), 2
        while self.get(key):
            key, i = f"{slugify(base)}-{i}", i + 1
        return key

    def add(self, key: str, name: str, ntype: str, tech: str, layer: int, description: str = "") -> str:
        key = self.new_key(key)
        self.comps.append({"key": key, "name": name, "type": ntype, "technology": tech, "description": description,
                           "layer": layer})
        return key

    def core(self, near: str = "") -> str | None:
        """The service that best fits ``near`` (a regex over name/technology), else the most connected service."""
        services = self.of_type("service", "agent")
        if near:
            for word in near.split("|"):  # the pattern's alternatives are in order of preference
                hit = next((c for c in services if re.search(word, f"{c['name']} {c.get('technology', '')}", re.I)), None)
                if hit:
                    return hit["key"]
        if services:
            return max(services, key=lambda c: self.degree(c["key"]))["key"]
        gws = self.of_type("gateway")
        return gws[0]["key"] if gws else (self.comps[0]["key"] if self.comps else None)

    def api_gateway(self) -> str | None:
        for c in self.of_type("gateway"):
            text = f"{c['name']} {c.get('technology', '')}"
            if not REALTIME_RE.search(text) and not _STATIC_EDGE_RE.search(text):
                return c["key"]
        return None

    def auth(self) -> str | None:
        return next((c["key"] for c in self.comps if is_auth(self.ns(c))), None)

    def bus(self) -> str | None:
        queues = self.of_type("queue")
        streams = [q for q in queues
                   if re.search(r"kafka|kinesis|event|stream|pub/?sub|bus|hub", f"{q['name']} {q.get('technology', '')}", re.I)]
        pick = streams or queues
        return pick[0]["key"] if pick else None


def ensure_auth(opt: _Opt, cloud: str, fixes: list[str]) -> str:
    key = opt.auth()
    if key:
        return key
    key = opt.add("auth", "Identity & Auth", "security", CLOUD_AUTH[cloud], 1, "Sign-in, tokens, roles")
    fixes.append(f"Added 'Identity & Auth' ({CLOUD_AUTH[cloud]})")
    gw = opt.api_gateway()
    if gw:
        opt.link(gw, key, "OIDC")
    return key


def add_capability(opt: _Opt, cap_id: str, cloud: str, fixes: list[str]) -> bool:
    """Add (and wire) the checklist's default component for a capability, or describe it on an existing one."""
    cap = checklists.CAPABILITIES[cap_id]
    spec = cap.get("component")
    if not spec:
        desc = cap.get("describe")
        if not desc:
            return False
        target = opt.core(desc["near"])
        c = opt.get(target) if target else None
        if not c or desc["text"] in (c.get("description") or ""):
            return False
        c["description"] = f"{c.get('description', '').rstrip()} {desc['text']}".strip()
        fixes.append(f"'{c['name']}': {desc['text'][0].lower()}{desc['text'][1:]}")
        return True
    tech = spec["tech"].get(cloud) or spec["tech"]["agnostic"]
    key = opt.add(spec["key"], spec["name"], spec["type"], tech, spec["layer"], spec.get("description", ""))
    mode, core = spec["attach"], opt.core(spec.get("near", ""))
    clients = [c["key"] for c in opt.of_type("client")]
    if mode in ("called", "external", "storage", "guard"):
        if core and core != key:
            opt.link(core, key, {"storage": "read/write", "external": "HTTPS", "guard": "check"}.get(mode, "REST"))
    elif mode == "consumer":
        bus = opt.bus()
        if bus:
            opt.link(bus, key, "consume")
        elif core:
            opt.link(core, key, "REST")
    elif mode == "observer":
        sources = [c["key"] for c in opt.comps if c["type"] in ("service", "agent", "gateway") and c["key"] != key]
        for s in sources[:8]:
            opt.link(s, key, "OTLP telemetry")
    elif mode == "client":
        gw = opt.api_gateway() or core
        if gw:
            opt.link(key, gw, "HTTPS")
    elif mode == "cdn":
        for c in clients[:3]:
            opt.link(c, key, "HTTPS")
        storage = next((c["key"] for c in opt.of_type("storage")), None)
        if not storage:
            add_capability(opt, "media_storage", cloud, fixes)
            storage = next((c["key"] for c in opt.of_type("storage")), None)
        if storage:
            opt.link(key, storage, "origin read")
    elif mode == "keys":
        owners = {e["from"] for e in opt.o["connections"]
                  if (opt.get(e["to"]) or {}).get("type") in ("database", "storage")
                  and (opt.get(e["from"]) or {}).get("type") in ("service", "agent")}
        for s in sorted(owners)[:6] or ([core] if core else []):
            opt.link(s, key, "KMS API")
    elif mode == "realtime":
        for c in clients[:3]:
            opt.link(c, key, "WSS")
        opt.link(key, ensure_auth(opt, cloud, fixes), "token validation")
        bus = opt.bus()
        if bus:
            opt.link(bus, key, "subscribe")
        elif core and core != key:
            opt.link(core, key, "push updates")
    elif mode == "ingest":
        store = next((c["key"] for c in opt.comps if c["type"] == "database" and re.search(
            r"vector|embedding|pgvector|search", f"{c['name']} {c.get('technology', '')}", re.I)), None)
        if store:
            opt.link(key, store, "upsert embeddings")
        docs = next((c["key"] for c in opt.of_type("storage")), None)
        if docs:
            opt.link(key, docs, "read documents")
    elif mode == "tools":
        agent = next((c["key"] for c in opt.of_type("agent")), core)
        if agent:
            opt.link(agent, key, "MCP")
        svc = next((c["key"] for c in opt.of_type("service") if c["key"] != key), None)
        if svc:
            opt.link(key, svc, "REST")
    fixes.append(f"Added '{spec['name']}' ({tech}) for {cap['label'].lower()}")
    return True


def auto_fix(option: dict[str, Any], issues: list[dict[str, Any]], cloud: str | None = None,
             include_info_capabilities: bool = False) -> list[str]:
    """Repair what rules can repair, in place. Returns a description of each change."""
    opt = _Opt(option)
    cloud = cloud or infer_cloud(option)
    fixes: list[str] = []
    for i in issues:
        code, sev = i["code"], i["severity"]
        if code == "unauthenticated_entry":
            auth = ensure_auth(opt, cloud, fixes)
            for nid in i["node_ids"]:
                if opt.get(nid) and opt.link(nid, auth, "token validation"):
                    fixes.append(f"Connected '{opt.get(nid)['name']}' to '{opt.get(auth)['name']}' (token validation)")
        elif code == "realtime_return_path":
            for nid in i["node_ids"]:
                rt = opt.get(nid)
                if not rt:
                    continue
                into = [e["to"] for e in opt.o["connections"] if e["from"] == nid and (opt.get(e["to"]) or {}).get("type") == "queue"]
                src = (into or [opt.bus()])[0] or opt.core(r"track|deliver|location|dispatch|order|notif")
                if src and opt.link(src, nid, "subscribe" if (opt.get(src) or {}).get("type") == "queue" else "push updates"):
                    fixes.append(f"Added return path '{opt.get(src)['name']}' → '{rt['name']}' so clients receive live updates")
        elif code == "missing_key_management":
            if not any(is_key_management(opt.ns(c)) for c in opt.comps):
                add_capability(opt, "key_management", cloud, fixes)
        elif code == "no_buffer_for_scale":
            if not opt.of_type("cache", "queue"):
                core = opt.core()
                key = opt.add("cache", "Cache", "cache", CLOUD_CACHE[cloud], 3, "Hot reads and rate limiting")
                if core:
                    opt.link(core, key, "RESP")
                fixes.append(f"Added 'Cache' ({CLOUD_CACHE[cloud]}) to absorb peak load")
        elif code == "completeness" and i.get("capability"):
            cap = checklists.CAPABILITIES[i["capability"]]
            if sev != "info" or include_info_capabilities or (not cap.get("component") and cap.get("describe")):
                add_capability(opt, i["capability"], cloud, fixes)
    return fixes


def review_option(option: dict[str, Any], requirements: list[dict[str, Any]], context: str = "",
                  cloud: str | None = None, include_info_capabilities: bool = False,
                  before: list[dict[str, Any]] | None = None, prior_fixes: list[str] | None = None,
                  engine: str = "rules") -> dict[str, Any]:
    """Normalize, check and rule-repair an option in place; sets and returns ``option["review"]``.

    ``before``/``prior_fixes`` carry the state from an earlier (LLM) repair so the review shows the whole story."""
    fixes = list(prior_fixes or [])
    fixes += normalize_option(option)
    issues = check_option(option, requirements, context)
    first = before if before is not None else issues
    for _ in range(2):  # adding a component can surface a follow-up issue (e.g. its own wiring); two passes suffice
        changed = auto_fix(option, issues, cloud, include_info_capabilities)
        if not changed:
            break
        fixes += changed
        issues = check_option(option, requirements, context)
    option["review"] = {"engine": engine, "issues_before": brief(first), "issues_after": brief(issues),
                        "fixes": fixes, "score": max(0, 100 - weight(blocking(issues)))}
    return option["review"]


# --------------------------------------------------------------------------- requirements breadth
NFR_STUBS = {
    "observability": {"title": "Operational observability", "kind": "non_functional", "category": "observability",
                      "priority": "high", "description": "Every service emits metrics, structured logs and traces.",
                      "acceptance_criteria": ["Dashboards show p95 latency and error rate per service",
                                              "An alert fires within 5 minutes of an SLO breach",
                                              "100 % of failed requests have a trace"]},
    "availability": {"title": "Availability and recovery", "kind": "non_functional", "category": "availability",
                     "priority": "high", "description": "The customer-facing system stays available and recovers from failures.",
                     "acceptance_criteria": ["99.9 % monthly availability of customer-facing APIs",
                                             "RPO ≤ 5 minutes and RTO ≤ 30 minutes for primary data"]},
    "security": {"title": "Authenticated and authorized access", "kind": "non_functional", "category": "security",
                 "priority": "high", "description": "All entry points authenticate callers and authorize by role.",
                 "acceptance_criteria": ["Unauthenticated requests and socket connections are rejected",
                                         "Role checks are enforced on every API"]},
}


def requirement_additions(context: str, requirements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stubs for functional areas the detected domains expect and for core NFRs, that ``requirements`` lack."""
    if not checklists.detect_domains(context + " " + " ".join(r.get("title", "") for r in requirements)):
        return []
    text = context + " " + " ".join(f"{r.get('title', '')} {r.get('description', '')}" for r in requirements)
    out = [checklists.requirement_stub(a) for a in checklists.missing_areas(text, requirements)]
    state, _ = apply_ops(ProjectState(), [{"op": "add_requirement", "requirement": {
        "title": r.get("title") or "R", "description": r.get("description", ""),
        "kind": r.get("kind") if r.get("kind") in ("functional", "non_functional") else "functional",
        "category": r.get("category", ""), "acceptance_criteria": r.get("acceptance_criteria") or []}}
        for r in requirements])
    covered = nfr_coverage(state)
    out += [dict(stub, source="checklist") for cat, stub in NFR_STUBS.items() if cat not in covered]
    titles = {r.get("title", "").strip().lower() for r in requirements}
    return [r for r in out if r["title"].lower() not in titles]
