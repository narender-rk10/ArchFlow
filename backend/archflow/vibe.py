"""Vibe-coding prompt generator.

Turns a selection (components and/or requirements) plus the surrounding project model into a
structured implementation brief for a coding agent (Claude Code, Codex, Gemini CLI, Aider, ...).
"""

from __future__ import annotations

from dataclasses import dataclass

from .analysis import trace_requirement
from .models import Edge, ProjectState

TARGETS = {
    "claude-code": "Claude Code",
    "codex": "OpenAI Codex CLI",
    "gemini-cli": "Gemini CLI",
    "aider": "Aider",
    "generic": "a coding agent",
}


@dataclass
class Scope:
    """What a brief covers: the components and requirements to build, the connections touching them, and the
    neighbouring components that are only integrated with (stubbed)."""
    node_ids: list[str]
    requirement_ids: list[str]
    whole_system: bool
    edges: list[Edge]
    boundary: list[str]


def resolve_scope(state: ProjectState, node_ids: list[str], requirement_ids: list[str]) -> Scope:
    """Expand a selection: requirements pull in the components tracing to them, components pull in the
    requirements they implement; nothing selected means the whole system. Notes are never in scope."""
    node_ids = [n for n in node_ids if n in state.nodes]
    requirement_ids = [r for r in requirement_ids if r in state.requirements]
    for rid in requirement_ids:
        for nid in trace_requirement(state, rid)["node_ids"]:
            if nid not in node_ids:
                node_ids.append(nid)
    for nid in node_ids:
        for rid in state.nodes[nid].requirement_ids:
            if rid in state.requirements and rid not in requirement_ids:
                requirement_ids.append(rid)
    whole = not node_ids and not requirement_ids
    if whole:
        node_ids = [n for n in state.nodes if state.nodes[n].type != "note"]
        requirement_ids = list(state.requirements)
    selected = set(node_ids)
    edges = [e for e in state.edges.values() if e.source in selected or e.target in selected]
    boundary = sorted(({e.source for e in edges} | {e.target for e in edges}) - selected)
    return Scope(node_ids, requirement_ids, whole, edges, boundary)


def build_prompt(state: ProjectState, project_name: str, node_ids: list[str], requirement_ids: list[str],
                 target: str = "claude-code", extra: str = "") -> str:
    node_ids = [n for n in node_ids if n in state.nodes]
    requirement_ids = [r for r in requirement_ids if r in state.requirements]

    # A requirement selection pulls in every component that traces to it.
    for rid in requirement_ids:
        for nid in trace_requirement(state, rid)["node_ids"]:
            if nid not in node_ids:
                node_ids.append(nid)
    # A component selection pulls in the requirements it implements.
    for nid in node_ids:
        for rid in state.nodes[nid].requirement_ids:
            if rid in state.requirements and rid not in requirement_ids:
                requirement_ids.append(rid)

    scope_all = not node_ids and not requirement_ids
    if scope_all:
        node_ids = list(state.nodes)
        requirement_ids = list(state.requirements)

    selected = set(node_ids)
    edges = [e for e in state.edges.values() if e.source in selected or e.target in selected]
    boundary = sorted(({e.source for e in edges} | {e.target for e in edges}) - selected)
    tech = sorted({state.nodes[n].technology for n in node_ids if state.nodes[n].technology})
    agent = TARGETS.get(target, TARGETS["generic"])

    out: list[str] = [f"# Implementation brief for {agent}", ""]
    out += ["## Project context",
            f"Project: **{project_name}**. The architecture below is the approved design stored in ArchFlow; "
            "implement it faithfully and flag any deviation you believe is necessary instead of silently changing it.", ""]
    out += ["## Scope", "Build the **entire system**." if scope_all else
            "Build **only** the components listed below. Other components are external dependencies: "
            "integrate with them through the listed interfaces and stub them in tests.", ""]

    out.append("## Components to implement")
    for nid in node_ids:
        n = state.nodes[nid]
        line = f"- **{n.name}** (`{n.type}`" + (f", {n.technology}" if n.technology else "") + ")"
        if n.description:
            line += f" — {n.description}"
        if n.requirement_ids:
            line += f"  _implements {', '.join(n.requirement_ids)}_"
        out.append(line)
        for k, v in n.props.items():
            out.append(f"  - {k}: {v}")
    out.append("")

    if edges:
        out.append("## Interfaces and dependencies")
        for e in edges:
            s, t = state.nodes[e.source], state.nodes[e.target]
            out.append(f"- {s.name} → {t.name}" + (f" via {e.protocol}" if e.protocol else "") + (f": {e.label}" if e.label else ""))
        out.append("")
    if boundary and not scope_all:
        out.append("## External components (do not implement, integrate/stub only)")
        out += [f"- {state.nodes[b].name} ({state.nodes[b].type}{', ' + state.nodes[b].technology if state.nodes[b].technology else ''})"
                for b in boundary]
        out.append("")

    if requirement_ids:
        out.append("## Requirements")
        for rid in requirement_ids:
            r = state.requirements[rid]
            out.append(f"### {r.id}: {r.title}  ({r.kind.replace('_', '-')}, priority {r.priority})")
            if r.description:
                out.append(r.description)
            if r.actors:
                out.append(f"Actors: {', '.join(r.actors)}")
            if r.acceptance_criteria:
                out.append("Acceptance criteria:")
                out += [f"- [ ] {c}" for c in r.acceptance_criteria]
            out.append("")

    out.append("## Technology and conventions")
    out.append(f"- Use the technologies named on each component{': ' + ', '.join(tech) if tech else ''}.")
    out += ["- Follow the existing repository's conventions, layout and tooling if a codebase already exists.",
            "- Keep configuration in environment variables; never hard-code secrets.",
            "- Add structured logging and health-check endpoints for every service.", ""]
    out += ["## Tests",
            "- Write tests that map one-to-one to the acceptance criteria above; name each test after its requirement ID.",
            "- Unit-test business logic; integration-test every interface listed above (stub external components).", ""]
    if extra:
        out += ["## Additional instructions", extra, ""]
    out += ["## Expected output",
            "1. A short plan listing files to create or change.",
            "2. The implementation.",
            "3. Tests, and the command to run them.",
            "4. A summary mapping each requirement ID to the code and tests that satisfy it."]
    return "\n".join(out)
