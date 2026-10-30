"""Spec exports and coding-agent guide files, all derived from the project model.

Formats (every one is built from the branch's state, its decisions, the project memory and versions; nothing is
a fixed template, and the same inputs always give the same bytes):

- ``spec_markdown``  the branch as a readable specification: overview, requirements (FR/NFR with acceptance
                     criteria, priority, status and the components that implement them), architecture (components
                     by type, with technology, description and connections), low-level designs, ADRs, project
                     memory and a health summary;
- ``spec_html``      the same document as print-ready HTML (light background, page breaks between chapters). Use
                     the browser's "Save as PDF": a PDF library that renders this well needs system libraries
                     (WeasyPrint) or bundled fonts (fpdf2), so the API serves the print-ready HTML instead;
- ``project_document`` → ``to_json`` / ``to_yaml``  machine-readable full export (``schema`` / ``schema_version``,
                     see SCHEMA_VERSION), read back with ``load_document``;
- ``guide_files``    CLAUDE.md, AGENTS.md and .cursorrules for coding agents: a skeleton derived from the model
                     (stack, services and their responsibilities, data stores, NFRs as constraints, accepted ADRs
                     as rules, a folder layout derived from the components) plus project-specific guidance
                     (conventions, do / don't, testing) written by the ``exports.agent_guide`` agent. Without an
                     AI model the skeleton is served alone.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import yaml
from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel

from . import agents, lld, llm
from .analysis import diagram_of, full_analysis, traceability
from .change_spec import slug
from .models import LLD_TYPES, Node, ProjectState, Requirement

log = logging.getLogger("archflow.exports")

PROMPT = "exports.agent_guide"  # prompts/exports.agent_guide.yaml (versioned; see agents.prompts)
SCHEMA_ID = "archflow.project"
# Bump when a field of the JSON/YAML export changes meaning or is removed (adding fields keeps the version).
SCHEMA_VERSION = 1
GUIDE_FILES = ("CLAUDE.md", "AGENTS.md", ".cursorrules")
GUIDE_KEYS = {"claude-md": "CLAUDE.md", "agents-md": "AGENTS.md", "cursorrules": ".cursorrules"}
MAX_GUIDE_ITEMS = 8

TYPE_ORDER = ("client", "gateway", "security", "service", "agent", "mcp", "llm", "queue", "cache", "database",
              "storage", "external", "note")
TYPE_LABELS = {"client": "Clients", "gateway": "Gateways", "security": "Security & identity", "service": "Services",
               "agent": "AI agents", "mcp": "MCP servers", "llm": "Language models", "queue": "Queues & streams",
               "cache": "Caches", "database": "Databases", "storage": "Storage", "external": "External systems",
               "note": "Notes"}
# How a component maps to code: built by the team (folder under apps/ or services/), configured (infra/), or used
# as it is (an integration, no folder).
CODE_KINDS = {"client": "apps", "service": "services", "agent": "services", "mcp": "services", "component": "services",
              "gateway": "infra", "security": "infra", "database": "infra", "cache": "infra", "queue": "infra",
              "storage": "infra"}
DATA_TYPES = ("database", "cache", "queue", "storage")
INTEGRATION_TYPES = ("external", "llm")


# --------------------------------------------------------------------------- what an export is built from
@dataclass
class Bundle:
    project: dict[str, Any]
    branch: str
    state: ProjectState
    head_version: int | None = None
    decisions: list[dict[str, Any]] = field(default_factory=list)
    memory: list[dict[str, Any]] = field(default_factory=list)
    versions: list[dict[str, Any]] = field(default_factory=list)
    branches: list[dict[str, Any]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.project.get("name") or "Untitled project"


def collect(store: Any, pid: str, branch: str, state: ProjectState | None = None) -> Bundle:
    """Everything an export needs; ``state`` defaults to the branch's working state."""
    project = store.get_project(pid) or {"id": pid, "name": pid}
    if state is None:
        state = store.get_state(pid, branch)
        if state is None:
            raise KeyError(f"branch '{branch}' not found")
    return Bundle(project=project, branch=branch, state=state, head_version=store.head_version(pid, branch),
                  decisions=store.list_decisions(pid), memory=store.list_memory(pid),
                  versions=store.list_versions(pid), branches=store.list_branches(pid))


# --------------------------------------------------------------------------- model helpers
def _natural(text: str) -> list[Any]:
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", text or "")]


def _top(state: ProjectState) -> list[Node]:
    """Top-level (HLD) components, by type then name."""
    order = {t: i for i, t in enumerate(TYPE_ORDER)}
    return sorted((n for n in state.nodes.values() if not diagram_of(state, n) and n.type not in LLD_TYPES),
                  key=lambda n: (order.get(n.type, len(order)), n.name.lower(), n.id))


def _requirements(state: ProjectState, kind: str | None = None) -> list[Requirement]:
    reqs = [r for r in state.requirements.values() if kind is None or r.kind == kind]
    return sorted(reqs, key=lambda r: _natural(r.id))


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def _cell(text: str) -> str:
    return _one_line(text).replace("|", "\\|") or "—"


def _with_tech(n: Node) -> str:
    return f"{n.name} ({n.technology})" if n.technology else n.name


def _node_ref(state: ProjectState, nid: str) -> str:
    n = state.nodes.get(nid)
    return n.name if n else f"`{nid}` (removed)"


def _req_ref(state: ProjectState, rid: str) -> str:
    r = state.requirements.get(rid)
    return f"{rid} {r.title}" if r else f"{rid} (removed)"


def _edges(state: ProjectState) -> list[Any]:
    """Connections ordered by what they join (not by generated ids), so equal models export equal text."""
    def name(nid: str) -> str:
        return state.nodes[nid].name.lower() if nid in state.nodes else "~" + nid

    return sorted(state.edges.values(), key=lambda e: (name(e.source), name(e.target), e.protocol, e.label, e.id))


def _links(state: ProjectState, node: Node) -> tuple[list[str], list[str]]:
    """(outgoing, incoming) connections of a component, as text; connections to removed components are skipped."""
    out, inc = [], []
    for e in _edges(state):
        via = ", ".join(x for x in (e.protocol, e.label) if x)
        suffix = f" ({via})" if via else ""
        if e.source == node.id and e.target in state.nodes:
            out.append(f"{state.nodes[e.target].name}{suffix}")
        elif e.target == node.id and e.source in state.nodes:
            inc.append(f"{state.nodes[e.source].name}{suffix}")
    return out, inc


def _implemented_by(state: ProjectState) -> dict[str, list[str]]:
    """Requirement id → components implementing it; low-level elements read "Parent › element"."""
    def label(nid: str) -> str:
        n = state.nodes[nid]
        parent = diagram_of(state, n)
        return f"{state.nodes[parent].name} › {n.name}" if parent else n.name

    return {rid: [label(n) for n in nodes] for rid, nodes in traceability(state)["requirement_to_nodes"].items()}


# --------------------------------------------------------------------------- Markdown spec
def spec_markdown(b: Bundle) -> str:
    s = b.state
    out: list[str] = []
    w = out.append
    version = f"v{b.head_version} (plus unsaved changes since)" if b.head_version else "no saved version yet"
    w(f"# {b.name}: architecture specification\n")
    w(f"Branch `{b.branch}` · {version} · model revision {s.rev}\n")
    if b.project.get("description"):
        w(_one_line(b.project["description"]) + "\n")

    # ---- overview ----
    top = _top(s)
    fr, nfr = _requirements(s, "functional"), _requirements(s, "non_functional")
    w("## 1. Overview\n")
    w(f"- **Components:** {len([n for n in top if n.type != 'note'])}"
      + (f" (+ {sum(1 for n in s.nodes.values() if diagram_of(s, n))} low-level design elements)"
         if any(diagram_of(s, n) for n in s.nodes.values()) else ""))
    w(f"- **Connections:** {len(s.edges)}")
    w(f"- **Requirements:** {len(fr)} functional, {len(nfr)} non-functional")
    w(f"- **Architecture decisions:** {len(b.decisions)}"
      + (f" ({sum(1 for d in b.decisions if d.get('status') == 'accepted')} accepted)" if b.decisions else ""))
    techs = list(dict.fromkeys(n.technology for n in top if n.technology))
    if techs:
        w(f"- **Technologies:** {', '.join(techs)}")
    w("")

    # ---- requirements ----
    w("## 2. Requirements\n")
    if not s.requirements:
        w("_No requirements recorded yet._\n")
    impl = _implemented_by(s)
    for title, reqs in (("Functional requirements", fr), ("Non-functional requirements", nfr)):
        if not reqs:
            continue
        w(f"### {title}\n")
        for r in reqs:
            w(f"#### {r.id}: {_one_line(r.title)}\n")
            meta = [f"Priority: {r.priority}", f"Status: {r.status}"]
            if r.category:
                meta.append(f"Category: {r.category}")
            if r.actors:
                meta.append(f"Actors: {', '.join(r.actors)}")
            w("- " + " · ".join(meta))
            w("- Implemented by: " + (", ".join(impl.get(r.id) or []) or "_not traced to any component_"))
            w("")
            if r.description:
                w(_one_line(r.description) + "\n")
            if r.acceptance_criteria:
                w("Acceptance criteria:\n")
                out.extend(f"- [ ] {_one_line(c)}" for c in r.acceptance_criteria)
                w("")

    # ---- architecture ----
    w("## 3. Architecture\n")
    if not top:
        w("_No components yet._\n")
    by_type: dict[str, list[Node]] = {}
    for n in top:
        by_type.setdefault(n.type, []).append(n)
    for t, nodes in by_type.items():
        w(f"### {TYPE_LABELS.get(t, t.title())}\n")
        for n in nodes:
            w(f"#### {n.name}\n")
            facts = [f"Type: {n.type}", f"ID: `{n.id}`"]
            if n.technology:
                facts.insert(0, f"Technology: {n.technology}")
            if n.vendor:
                facts.append(f"Vendor: {n.vendor}")
            w("- " + " · ".join(facts))
            if n.requirement_ids:
                w("- Requirements: " + ", ".join(_req_ref(s, rid) for rid in n.requirement_ids))
            outgoing, incoming = _links(s, n)
            if outgoing:
                w("- Calls: " + ", ".join(outgoing))
            if incoming:
                w("- Called by: " + ", ".join(incoming))
            if n.props:
                w("- Properties: " + ", ".join(f"{k}={v}" for k, v in sorted(n.props.items())))
            w("")
            if n.description:
                w(_one_line(n.description) + "\n")
    top_ids = {n.id for n in top}
    conns = [e for e in _edges(s) if e.source in top_ids and e.target in top_ids]
    if conns:
        w("### Connections\n")
        w("| From | To | Protocol | Purpose |")
        w("| --- | --- | --- | --- |")
        out.extend(f"| {_cell(s.nodes[e.source].name)} | {_cell(s.nodes[e.target].name)} | {_cell(e.protocol)} | "
                   f"{_cell(e.label)} |" for e in conns)
        w("")
    dangling = [e.id for e in s.edges.values() if e.source not in s.nodes or e.target not in s.nodes]
    if dangling:
        w(f"_{len(dangling)} connection(s) point to removed components and are left out: {', '.join(sorted(dangling))}._\n")

    # ---- low-level designs ----
    designs = [n for n in _top(s) if lld.children(s, n.id)]
    w("## 4. Low-level designs\n")
    if not designs:
        w("_No component has a low-level design yet._\n")
    for n in designs:
        _lld_section(s, n, w)

    # ---- decisions ----
    w("## 5. Architecture decisions\n")
    if not b.decisions:
        w("_No decisions recorded yet._\n")
    for d in b.decisions:
        _adr_section(s, d, w)

    # ---- memory ----
    w("## 6. Project memory\n")
    if not b.memory:
        w("_No settled facts recorded yet._\n")
    else:
        w("Facts the team settled; every AI feature follows them.\n")
        out.extend(f"- **{m.get('category', 'fact')}**{' (pinned)' if m.get('pinned') else ''}: {_one_line(m.get('text', ''))}"
                   for m in b.memory)
        w("")

    # ---- health ----
    _health_section(s, w)
    return "\n".join(out).rstrip() + "\n"


def _lld_section(s: ProjectState, n: Node, w: Callable[[str], None]) -> None:
    kids = lld.children(s, n.id)
    w(f"### {n.name} internals\n")
    endpoints = lld.api_view(s, n)
    if endpoints:
        w("API:\n")
        w("| Method | Path | Description | Requirements |")
        w("| --- | --- | --- | --- |")
        for e in endpoints:
            w(f"| {e['method']} | `{e['path']}` | {_cell(e['description'])} | {_cell(', '.join(e['requirement_ids']))} |")
        w("")
    tables = [c for c in kids if c.type == "table"]
    for t in tables:
        cols = lld.parse_columns(t.props.get("columns", ""))
        w(f"Table `{t.name}`" + (f": {_one_line(t.description)}" if t.description else "") + "\n")
        if cols:
            w("| Column | Type | Key |")
            w("| --- | --- | --- |")
            for c in cols:
                w(f"| {_cell(c['name'])} | {_cell(c['type'])} | {_cell(c['key'])} |")
            w("")
    for label, t in (("Modules", "component"), ("Events", "event")):
        items = [c for c in kids if c.type == t]
        if items:
            w(f"{label}:\n")
            for c in items:
                extra = c.props.get("channel") if t == "event" else c.technology
                desc = f": {_one_line(c.description)}" if c.description else ""
                w(f"- **{c.name}**" + (f" ({extra})" if extra else "") + desc)
            w("")
    other = [c for c in kids if c.type not in LLD_TYPES]
    if other:
        w("Other elements: " + ", ".join(_with_tech(c) for c in other) + "\n")
    ids = {c.id for c in kids}
    flows = [e for e in _edges(s) if e.source in ids and e.target in ids]
    if flows:
        w("Internal flow:\n")
        for e in flows:
            via = ", ".join(x for x in (e.protocol, e.label) if x)
            w(f"- {s.nodes[e.source].name} → {s.nodes[e.target].name}" + (f" ({via})" if via else ""))
        w("")


def _adr_section(s: ProjectState, d: dict[str, Any], w: Callable[[str], None]) -> None:
    w(f"### {d.get('id', 'ADR')}: {_one_line(d.get('title', ''))}\n")
    w(f"- Status: **{d.get('status', 'proposed')}**" + (f" · Author: {d['author']}" if d.get("author") else ""))
    if d.get("requirement_ids"):
        w("- Requirements: " + ", ".join(_req_ref(s, r) for r in d["requirement_ids"]))
    if d.get("node_ids"):
        w("- Components: " + ", ".join(_node_ref(s, n) for n in d["node_ids"]))
    w("")
    for label, key in (("Context", "context"), ("Decision", "decision"), ("Consequences", "consequences")):
        if (d.get(key) or "").strip():
            w(f"**{label}.** {_one_line(d[key])}\n")
    alts = d.get("alternatives") or []
    if alts:
        w("Alternatives considered:\n")
        for a in alts:
            name = a.get("name", "?")
            chosen = " **(chosen)**" if name and name == d.get("chosen") else ""
            line = f"- {name}{chosen}" + (f": {_one_line(a['summary'])}" if a.get("summary") else "")
            w(line)
            if a.get("pros"):
                w("  - Pros: " + "; ".join(_one_line(p) for p in a["pros"]))
            if a.get("cons"):
                w("  - Cons: " + "; ".join(_one_line(c) for c in a["cons"]))
        w("")


def _health_section(s: ProjectState, w: Callable[[str], None]) -> None:
    a = full_analysis(s)
    h = a["health"]
    w("## 7. Health\n")
    w("| Measure | Value |")
    w("| --- | --- |")
    w(f"| Architecture score | {h['architecture_score']} / 100 |")
    w(f"| Requirements complete | {h['requirements_complete']}% |")
    w(f"| Requirements approved | {h['requirements_approved']}% |")
    w(f"| Requirements traced to components | {h['traceability']}% |")
    w(f"| Open issues | {h['issues']['errors']} errors, {h['issues']['warnings']} warnings, {h['issues']['info']} info |")
    w("")
    issues = [i for i in a["architecture_issues"] + a["requirement_issues"] if i["severity"] in ("error", "warning")]
    issues.sort(key=lambda i: (0 if i["severity"] == "error" else 1))
    if issues:
        w("Open errors and warnings:\n")
        for i in issues[:20]:
            w(f"- **{i['severity']}**: {_one_line(i['message'])}" + (f" {_one_line(i['suggestion'])}" if i["suggestion"] else ""))
        if len(issues) > 20:
            w(f"- … and {len(issues) - 20} more")
        w("")


# --------------------------------------------------------------------------- print-ready HTML (PDF via the browser)
PRINT_CSS = """
:root { color-scheme: light; }
body { background: #fff; color: #1a1d23; font: 11pt/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  max-width: 900px; margin: 0 auto; padding: 32px 24px; }
h1 { font-size: 22pt; margin: 0 0 4px; } h2 { font-size: 16pt; margin-top: 28px; border-bottom: 1px solid #d0d4da;
  padding-bottom: 4px; } h3 { font-size: 13pt; margin-top: 20px; } h4 { font-size: 11pt; margin: 14px 0 4px; }
h2, h3, h4 { break-after: avoid; page-break-after: avoid; }
table { border-collapse: collapse; width: 100%; margin: 6px 0 12px; font-size: 10pt; break-inside: avoid; }
th, td { border: 1px solid #d0d4da; padding: 4px 8px; text-align: left; vertical-align: top; }
th { background: #f3f5f8; }
code { background: #f3f5f8; padding: 0 3px; border-radius: 3px; font-size: 0.92em; }
pre { background: #f6f8fa; border: 1px solid #e1e4e8; padding: 8px 10px; white-space: pre-wrap; break-inside: avoid; }
ul { padding-left: 22px; } li { margin: 2px 0; }
.toolbar { margin-bottom: 16px; } .toolbar button { font: inherit; padding: 4px 12px; }
@page { margin: 18mm 16mm; }
@media print { .toolbar { display: none; } body { padding: 0; max-width: none; }
  h2 { break-before: page; page-break-before: always; } h2.first { break-before: auto; page-break-before: auto; } }
"""

_INLINE = re.compile(r"`([^`]+)`|\*\*(.+?)\*\*|(?<![A-Za-z0-9])_(.+?)_(?![A-Za-z0-9])")


def _inline(text: str) -> str:
    out, pos = [], 0
    for m in _INLINE.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        if m.group(1) is not None:
            out.append(f"<code>{html.escape(m.group(1))}</code>")
        elif m.group(2) is not None:
            out.append(f"<strong>{_inline(m.group(2))}</strong>")
        else:
            out.append(f"<em>{_inline(m.group(3))}</em>")
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def _row(line: str) -> list[str]:
    cells = re.split(r"(?<!\\)\|", line.strip().strip("|"))
    return [c.strip().replace("\\|", "|") for c in cells]


def markdown_to_html(md: str) -> str:
    """The Markdown subset the exports write (headings, paragraphs, nested lists, tables, code) as HTML."""
    lines = md.splitlines()
    out: list[str] = []
    i, first_h2 = 0, True
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            out.append("<pre>" + html.escape("\n".join(lines[i + 1:j])) + "</pre>")
            i = j + 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            cls = ' class="first"' if level == 2 and first_h2 else ""
            first_h2 = first_h2 and level != 2
            out.append(f"<h{level}{cls}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue
        if line.lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            head = _row(line)
            i += 2
            body = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                body.append(_row(lines[i]))
                i += 1
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body)
                       + "</tbody></table>")
            continue
        if re.match(r"^\s*- ", line):
            items: list[tuple[int, str]] = []
            while i < len(lines) and re.match(r"^\s*- ", lines[i]):
                indent = len(lines[i]) - len(lines[i].lstrip())
                items.append((indent // 2, lines[i].strip()[2:]))
                i += 1
            out.append(_list_html(items))
            continue
        para = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#|\s*- |\s*\||```)", lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.append(f"<p>{_inline(' '.join(para))}</p>")
    return "\n".join(out)


def _list_html(items: list[tuple[int, str]]) -> str:
    out, depth = [], -1
    for level, text in items:
        level = min(level, depth + 1)
        while depth < level:
            out.append("<ul>")
            depth += 1
        while depth > level:
            out.append("</li></ul>")
            depth -= 1
        if out[-1] != "<ul>":
            out.append("</li>")
        box = re.match(r"^\[( |x)\] (.*)$", text)
        body = (("☑ " if box.group(1) == "x" else "☐ ") + _inline(box.group(2))) if box else _inline(text)
        out.append(f"<li>{body}")
    while depth >= 0:
        out.append("</li></ul>")
        depth -= 1
    return "".join(out)


def spec_html(b: Bundle) -> str:
    title = html.escape(f"{b.name}: architecture specification")
    return (f"<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>{title}</title>"
            f"<style>{PRINT_CSS}</style></head><body>\n"
            "<div class=\"toolbar\"><button onclick=\"window.print()\">Print / Save as PDF</button></div>\n"
            f"{markdown_to_html(spec_markdown(b))}\n</body></html>\n")


# --------------------------------------------------------------------------- JSON / YAML
def project_document(b: Bundle) -> dict[str, Any]:
    """The full project as data. ``schema``/``schema_version`` identify the format (see SCHEMA_VERSION); ``state``
    is the branch's model exactly as ArchFlow stores it (``ProjectState``)."""
    p = b.project
    return {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "project": {"id": p.get("id"), "name": b.name, "description": p.get("description") or "",
                    "created_at": p.get("created_at")},
        "branch": b.branch,
        "head_version": b.head_version,
        "state": b.state.model_dump(mode="json"),
        "decisions": [{k: v for k, v in d.items()} for d in b.decisions],
        "memory": [{k: m.get(k) for k in ("id", "text", "category", "pinned", "source", "created_by", "created_at")}
                   for m in b.memory],
        "versions": [dict(v) for v in b.versions],
        "branches": [dict(br) for br in b.branches],
    }


def _plain(value: Any) -> Any:
    """JSON-safe copy (timestamps and other store values become strings)."""
    return json.loads(json.dumps(value, default=str))


def to_json(b: Bundle) -> str:
    return json.dumps(_plain(project_document(b)), indent=2, ensure_ascii=False) + "\n"


def to_yaml(b: Bundle) -> str:
    return yaml.safe_dump(_plain(project_document(b)), sort_keys=False, allow_unicode=True, width=120)


def load_document(data: dict[str, Any] | str) -> dict[str, Any]:
    """Read an export back (a dict, or JSON / YAML text); ``state`` becomes a ``ProjectState``. Missing optional
    sections read as empty. Raises ValueError for another format or a newer schema version."""
    if isinstance(data, str):
        data = yaml.safe_load(data)  # YAML is a superset of JSON
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_ID:
        raise ValueError(f"not an ArchFlow project export (expected schema '{SCHEMA_ID}')")
    version = data.get("schema_version")
    if not isinstance(version, int) or version < 1 or version > SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version {version!r} (this ArchFlow reads 1..{SCHEMA_VERSION})")
    return {**data, "state": ProjectState.model_validate(data.get("state") or {}),
            **{k: data.get(k) or [] for k in ("decisions", "memory", "versions", "branches")}}


# --------------------------------------------------------------------------- coding-agent guides
def _layout(b: Bundle) -> list[tuple[str, str]]:
    """(path, comment) of a repository layout derived from the components; folders end with '/'."""
    s = b.state
    entries: list[tuple[str, str]] = []
    used: set[str] = set()

    def folder(root: str, n: Node) -> str:
        base = slug(n.name, 40)
        name, k = base, 2
        while f"{root}/{name}" in used:
            name, k = f"{base}-{k}", k + 1
        used.add(f"{root}/{name}")
        return f"{root}/{name}/"

    contracts: list[tuple[str, str]] = []
    for n in _top(s):
        root = CODE_KINDS.get(n.type)
        if not root:
            continue
        path = folder(root, n)
        kids = lld.children(s, n.id)
        if root == "infra":
            what = {"database": "schema and migrations", "cache": "configuration", "queue": "topics and configuration",
                    "storage": "buckets and policies"}.get(n.type, "configuration")
            entries.append((path, f"{_with_tech(n)}: {what}"))
            tables = [c.name for c in kids if c.type == "table"]
            if tables:
                entries.append((f"{path}migrations/", f"tables: {', '.join(tables)}"))
            if n.type == "queue":
                contracts.append((f"contracts/events/{slug(n.name, 40)}/", f"message schemas on {n.name}"))
            continue
        entries.append((path, _with_tech(n)))
        modules = [c for c in kids if c.type == "component"]
        for c in modules:
            entries.append((f"{path}src/{slug(c.name, 40)}/", _with_tech(c)))
        if not modules:
            entries.append((f"{path}src/", ""))
        if any(c.type == "endpoint" for c in kids):
            contracts.append((f"contracts/openapi/{slug(n.name, 40)}.yaml", f"{n.name} API"))
        if any(c.type == "event" for c in kids):
            contracts.append((f"contracts/events/{slug(n.name, 40)}/",
                              "events: " + ", ".join(c.name for c in kids if c.type == "event")))
        reqs = [r for r in n.requirement_ids if r in s.requirements]
        entries.append((f"{path}tests/", f"covers {', '.join(reqs)}" if reqs else ""))
    entries += contracts
    entries.append(("docs/architecture.md", "exported from ArchFlow"))
    if b.decisions:
        entries.append(("docs/adr/", ", ".join(d.get("id", "") for d in b.decisions)))
    order = {r: i for i, r in enumerate(("apps", "services", "infra", "contracts", "docs"))}
    root = slug(b.name, 40) + "/"
    return [(root + p, c) for p, c in sorted(entries, key=lambda e: order[e[0].split("/")[0]])]  # stable sort


def _tree(entries: list[tuple[str, str]]) -> str:
    root: dict[str, Any] = {}
    comments: dict[tuple[str, ...], str] = {}
    for path, comment in entries:
        parts = tuple(p + "/" for p in path.rstrip("/").split("/")[:-1]) + (
            path.rstrip("/").split("/")[-1] + ("/" if path.endswith("/") else ""),)
        node = root
        for p in parts:
            node = node.setdefault(p, {})
        if comment:
            comments[parts] = comment
    lines: list[str] = []

    def walk(node: dict[str, Any], prefix: str, trail: tuple[str, ...]) -> None:
        keys = list(node)
        for i, k in enumerate(keys):
            last = i == len(keys) - 1
            note = comments.get(trail + (k,))
            lines.append(f"{prefix}{'└── ' if last else '├── '}{k}" + (f"  # {note}" if note else ""))
            walk(node[k], prefix + ("    " if last else "│   "), trail + (k,))

    for top in root:  # the repository folder itself, then its tree
        lines.append(top)
        walk(root[top], "", (top,))
    return "\n".join(lines)


def guide_context(b: Bundle) -> dict[str, Any]:
    """What the guide files say about the system, derived from the model (deterministic)."""
    s = b.state
    top = [n for n in _top(s) if n.type != "note"]
    stack: dict[str, list[str]] = {}
    for n in top:
        if n.technology:
            stack.setdefault(n.technology, []).append(n.name)
    impl = _implemented_by(s)

    def component(n: Node) -> dict[str, Any]:
        outgoing, incoming = _links(s, n)
        return {"name": n.name, "type": n.type, "technology": n.technology, "description": _one_line(n.description),
                "responsibilities": [f"{r} {s.requirements[r].title}" for r in n.requirement_ids if r in s.requirements],
                "calls": outgoing, "called_by": incoming,
                "internals": [_with_tech(c) for c in lld.children(s, n.id)][:12]}

    return {
        "project": b.name, "description": _one_line(b.project.get("description") or ""), "branch": b.branch,
        "version": b.head_version,
        "stack": [{"technology": t, "used_by": names} for t, names in stack.items()],
        "services": [component(n) for n in top if n.type in ("client", "gateway", "security", "service", "agent", "mcp")],
        "data_stores": [component(n) for n in top if n.type in DATA_TYPES],
        "integrations": [component(n) for n in top if n.type in INTEGRATION_TYPES],
        "constraints": [{"id": r.id, "category": r.category, "title": _one_line(r.title), "priority": r.priority,
                         "criteria": [_one_line(c) for c in r.acceptance_criteria],
                         "components": impl.get(r.id) or []} for r in _requirements(s, "non_functional")],
        "rules": [{"id": d.get("id", ""), "title": _one_line(d.get("title", "")),
                   "decision": _one_line(d.get("decision") or d.get("chosen") or "")}
                  for d in b.decisions if d.get("status") == "accepted"],
        "facts": [{"category": m.get("category", "fact"), "text": _one_line(m.get("text", ""))} for m in b.memory],
        "layout": _tree(_layout(b)),
    }


GUIDANCE_SCHEMA: dict[str, Any] = {"type": "object", "properties": {
    "conventions": {"type": "array", "items": {"type": "string"}},
    "do": {"type": "array", "items": {"type": "string"}},
    "dont": {"type": "array", "items": {"type": "string"}},
    "testing": {"type": "array", "items": {"type": "string"}}},
    "required": ["conventions", "do", "dont", "testing"]}
GUIDANCE_SECTIONS = (("conventions", "Conventions"), ("do", "Do"), ("dont", "Don't"), ("testing", "Testing"))


def _skeleton_md(ctx: dict[str, Any]) -> str:
    """The derived part of CLAUDE.md / AGENTS.md (also the agent's input)."""
    out: list[str] = []
    w = out.append
    w("## System\n")
    w(f"{ctx['project']}" + (f": {ctx['description']}" if ctx["description"] else "") + "\n")
    if ctx["stack"]:
        w("## Stack\n")
        out.extend(f"- {x['technology']}: {', '.join(x['used_by'])}" for x in ctx["stack"])
        w("")

    def comp_lines(items: list[dict[str, Any]]) -> None:
        for c in items:
            head = f"- **{c['name']}**" + (f" ({c['technology']})" if c["technology"] else "") + f" [{c['type']}]"
            w(head + (f": {c['description']}" if c["description"] else ""))
            if c["responsibilities"]:
                w(f"  - Implements: {'; '.join(c['responsibilities'])}")
            if c["calls"]:
                w(f"  - Calls: {', '.join(c['calls'])}")
            if c["called_by"]:
                w(f"  - Called by: {', '.join(c['called_by'])}")
            if c["internals"]:
                w(f"  - Internals: {', '.join(c['internals'])}")
        w("")

    for key, title in (("services", "Services and their responsibilities"), ("data_stores", "Data stores"),
                       ("integrations", "External integrations")):
        if ctx[key]:
            w(f"## {title}\n")
            comp_lines(ctx[key])
    if ctx["constraints"]:
        w("## Constraints (non-functional requirements)\n")
        for c in ctx["constraints"]:
            w(f"- **{c['id']}** {c['title']}" + (f" [{c['category']}]" if c["category"] else "")
              + (f", applies to {', '.join(c['components'])}" if c["components"] else ""))
            out.extend(f"  - {x}" for x in c["criteria"])
        w("")
    if ctx["rules"]:
        w("## Rules (accepted architecture decisions)\n")
        out.extend(f"- **{r['id']}** {r['title']}" + (f": {r['decision']}" if r["decision"] else "") for r in ctx["rules"])
        w("")
    if ctx["facts"]:
        w("## Settled facts\n")
        out.extend(f"- [{f['category']}] {f['text']}" for f in ctx["facts"])
        w("")
    w("## Suggested layout\n")
    w("```")
    w(ctx["layout"])
    w("```\n")
    return "\n".join(out)


def _guidance_md(guidance: dict[str, list[str]] | None) -> str:
    if not guidance:
        return ""
    out = []
    for key, title in GUIDANCE_SECTIONS:
        if guidance.get(key):
            out.append(f"## {title}\n")
            out.extend(f"- {x}" for x in guidance[key])
            out.append("")
    return "\n".join(out)


def render_guide(filename: str, ctx: dict[str, Any], guidance: dict[str, list[str]] | None) -> str:
    version = f"v{ctx['version']}" if ctx["version"] else "unsaved working state"
    source = f"the ArchFlow design of {ctx['project']} (branch `{ctx['branch']}`, {version})"
    if filename == ".cursorrules":
        return _cursorrules(ctx, guidance, source)
    reader = "Claude Code" if filename == "CLAUDE.md" else "coding agents (Codex, Gemini CLI, Cursor, Aider and others)"
    head = (f"# {filename}\n\nGuidance for {reader} working in this repository, generated from {source}. The "
            "architecture there is the source of truth: when the code needs to differ, change the design first.\n")
    return "\n".join(x for x in (head, _skeleton_md(ctx), _guidance_md(guidance)) if x).rstrip() + "\n"


def _cursorrules(ctx: dict[str, Any], guidance: dict[str, list[str]] | None, source: str) -> str:
    out = [f"You are working on {ctx['project']}. These rules come from {source}.", ""]
    if ctx["stack"]:
        out.append("Stack: " + "; ".join(f"{x['technology']} ({', '.join(x['used_by'])})" for x in ctx["stack"]) + ".")
    for c in ctx["services"] + ctx["data_stores"]:
        resp = f" Implements {'; '.join(c['responsibilities'])}." if c["responsibilities"] else ""
        calls = f" Calls {', '.join(c['calls'])}." if c["calls"] else ""
        out.append(f"- {c['name']}" + (f" ({c['technology']})" if c["technology"] else "") + f".{resp}{calls}")
    if ctx["integrations"]:
        out.append("External integrations: " + ", ".join(
            c["name"] + (f" ({c['technology']})" if c["technology"] else "") for c in ctx["integrations"]) + ".")
    if ctx["constraints"]:
        out += ["", "Constraints:"]
        for c in ctx["constraints"]:
            crit = f" ({'; '.join(c['criteria'])})" if c["criteria"] else ""
            out.append(f"- {c['id']} {c['title']}{crit}")
    if ctx["rules"]:
        out += ["", "Rules from accepted decisions:"]
        out += [f"- {r['id']}: {r['decision'] or r['title']}" for r in ctx["rules"]]
    if ctx["facts"]:
        out += ["", "Settled facts:"] + [f"- {f['text']}" for f in ctx["facts"]]
    if guidance:
        for key, title in GUIDANCE_SECTIONS:
            if guidance.get(key):
                out += ["", f"{title}:"] + [f"- {x}" for x in guidance[key]]
    out += ["", "Repository layout:", ctx["layout"]]
    return "\n".join(out).rstrip() + "\n"


def _clean_guidance(data: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, _ in GUIDANCE_SECTIONS:
        seen, items = set(), []
        for x in data.get(key) or []:
            text = _one_line(str(x)).lstrip("-• ").strip()[:400]
            if text and text.lower() not in seen:
                seen.add(text.lower())
                items.append(text)
        out[key] = items[:MAX_GUIDE_ITEMS]
    return out


def facts_text(ctx: dict[str, Any]) -> str:
    """The project memory as the agent's ``memory`` variable."""
    return "\n".join(f"- [{f['category']}] {f['text']}" for f in ctx["facts"])


def agent_guidance(ctx: dict[str, Any], memory: str = "", org_id: str | None = None) -> dict[str, list[str]]:
    """Project-specific conventions, do/don't and testing approach, written by the ``exports.agent_guide`` agent."""
    data = agents.run_json(PROMPT, GUIDANCE_SCHEMA, org_id=org_id, question=f"Coding-agent guide for {ctx['project']}",
                           variables={"project_name": ctx["project"], "context": _skeleton_md(ctx), "memory": memory})
    guidance = _clean_guidance(data)
    if not any(guidance.values()):
        raise llm.LLMInvalidOutput("the model returned no guidance")
    return guidance


def guide_files(b: Bundle, *, use_llm: bool = True, org_id: str | None = None) -> dict[str, Any]:
    """{files: {CLAUDE.md, AGENTS.md, .cursorrules}, engine: "llm" | "offline", notes}. One agent run feeds all three."""
    ctx = guide_context(b)
    guidance, engine, notes = None, "offline", []
    if use_llm and b.state.nodes and llm.available(org_id):
        try:
            guidance, engine = agent_guidance(ctx, facts_text(ctx), org_id), "llm"
        except (llm.LLMUnavailable, llm.LLMRefused, llm.LLMInvalidOutput) as exc:
            log.info("agent guide failed: %s", exc)
            notes.append(f"The AI guidance could not be written ({exc}); the files hold the derived skeleton only.")
    elif use_llm and b.state.nodes:
        notes.append("No AI model is configured: the files hold the derived skeleton only.")
    return {"files": {f: render_guide(f, ctx, guidance) for f in GUIDE_FILES}, "engine": engine, "notes": notes}


# --------------------------------------------------------------------------- REST
class GuidesIn(BaseModel):
    use_ai: bool = True


def _filename(*parts: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", "-".join(p for p in parts if p)).strip("-")[:80] or "project"


def register_routes(app: FastAPI, store: Any, *, authz: Any,
                    state_or_404: Callable[[str, str], ProjectState]) -> None:
    """Read-only downloads: any role that can read the project (viewer and above)."""
    read = authz.project("read")

    def bundle(pid: str, branch: str) -> Bundle:
        return collect(store, pid, branch, state_or_404(pid, branch))

    def file(content: str, media: str, name: str, inline: bool = False) -> Response:
        disposition = "inline" if inline else "attachment"
        return Response(content, media_type=media, headers={
            "Content-Disposition": f'{disposition}; filename="{name}"', "Cache-Control": "no-store"})

    @app.get("/api/projects/{pid}/export/spec.md")
    def export_markdown(pid: str, branch: str = "main", a=Depends(read)):
        b = bundle(pid, branch)
        return file(spec_markdown(b), "text/markdown; charset=utf-8", _filename(b.name, branch, "spec") + ".md")

    @app.get("/api/projects/{pid}/export/spec.html")
    def export_print(pid: str, branch: str = "main", inline: bool = False, a=Depends(read)):
        """Print-ready HTML of the spec: open it and use the browser's Save as PDF."""
        b = bundle(pid, branch)
        return file(spec_html(b), "text/html; charset=utf-8", _filename(b.name, branch, "spec") + ".html", inline)

    @app.get("/api/projects/{pid}/export/project.json")
    def export_json(pid: str, branch: str = "main", a=Depends(read)):
        b = bundle(pid, branch)
        return file(to_json(b), "application/json", _filename(b.name, branch) + ".archflow.json")

    @app.get("/api/projects/{pid}/export/project.yaml")
    def export_yaml(pid: str, branch: str = "main", a=Depends(read)):
        b = bundle(pid, branch)
        return file(to_yaml(b), "application/yaml; charset=utf-8", _filename(b.name, branch) + ".archflow.yaml")

    @app.post("/api/projects/{pid}/export/guides")
    async def export_guides(pid: str, body: GuidesIn, branch: str = "main", a=Depends(read)):
        """CLAUDE.md, AGENTS.md and .cursorrules for preview; ``use_ai`` adds the agent's project guidance."""
        b = bundle(pid, branch)
        return await asyncio.to_thread(guide_files, b, use_llm=body.use_ai, org_id=a.org_id)

    @app.get("/api/projects/{pid}/export/guides/{key}")
    async def export_guide(pid: str, key: str, branch: str = "main", ai: bool = False, a=Depends(read)):
        """One guide file as a download (``key``: claude-md, agents-md or cursorrules)."""
        name = GUIDE_KEYS.get(key)
        if not name:
            raise HTTPException(404, f"unknown guide '{key}' (one of {', '.join(GUIDE_KEYS)})")
        res = await asyncio.to_thread(guide_files, bundle(pid, branch), use_llm=ai, org_id=a.org_id)
        return file(res["files"][name], "text/markdown; charset=utf-8" if name.endswith(".md") else "text/plain; charset=utf-8",
                    name)
