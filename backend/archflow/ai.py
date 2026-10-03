"""AI layer: natural-language commands → structured operations, and questions about the project.

The model edits the canvas through the same tools an MCP agent would use (add_component,
connect_components, ...). Tool calls are collected and applied as one atomic batch through
``ops.apply_ops`` — the AI never produces a picture, only operations on the shared model.

All model calls go through the provider-agnostic gateway in ``llm.py`` (LiteLLM Router: Gemini
primary, Claude and OpenAI as fallbacks). If no provider key is configured (or ``ARCHFLOW_AI=offline``),
a small rule-based parser handles common canvas commands so the product still works offline.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from . import llm
from .models import NODE_TYPES, ProjectState
from .ops import resolve_node

log = logging.getLogger("archflow.ai")

MODE = os.environ.get("ARCHFLOW_AI", "auto")  # auto | claude (or llm: never fall back) | offline
# Reported by /api/health: the primary model of the "reasoning" group (see llm.py for the fallback chain).
MODEL = llm.primary_model("reasoning")
MAX_TOKENS = int(os.environ.get("ARCHFLOW_MAX_TOKENS", "16000"))


@dataclass
class CommandResult:
    ops: list[dict[str, Any]] = field(default_factory=list)
    explanation: str = ""
    questions: list[str] = field(default_factory=list)
    engine: str = "offline"


# --------------------------------------------------------------------------- tools
_STR = {"type": "string"}
_STRS = {"type": "array", "items": {"type": "string"}}

_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "add_component",
        "description": "Add an architecture component to the canvas. Refer to it later in the same response by its name.",
        "parameters": {"type": "object", "properties": {
            "name": {**_STR, "description": "Display name, e.g. 'Order Service'."},
            "type": {"type": "string", "enum": list(NODE_TYPES)},
            "technology": {**_STR, "description": "e.g. 'FastAPI', 'PostgreSQL', 'Redis'."},
            "description": _STR,
            "x": {"type": "number"}, "y": {"type": "number"},
            "requirement_ids": {**_STRS, "description": "Requirement IDs this component helps satisfy."},
        }, "required": ["name", "type"]},
    },
    {
        "name": "update_component",
        "description": "Change properties of an existing component (rename, change technology, move, relink requirements).",
        "parameters": {"type": "object", "properties": {
            "component": {**_STR, "description": "Existing component id or name."},
            "name": _STR, "type": {"type": "string", "enum": list(NODE_TYPES)}, "technology": _STR,
            "description": _STR, "x": {"type": "number"}, "y": {"type": "number"}, "requirement_ids": _STRS,
        }, "required": ["component"]},
    },
    {
        "name": "remove_component",
        "description": "Remove a component and all of its connections.",
        "parameters": {"type": "object", "properties": {"component": _STR}, "required": ["component"]},
    },
    {
        "name": "connect_components",
        "description": "Create a directed connection (data/call flow) from source to target.",
        "parameters": {"type": "object", "properties": {
            "source": _STR, "target": _STR,
            "protocol": {**_STR, "description": "e.g. REST, gRPC, SQL, Kafka, MCP, A2A."}, "label": _STR,
        }, "required": ["source", "target"]},
    },
    {
        "name": "disconnect_components",
        "description": "Remove the connection from source to target.",
        "parameters": {"type": "object", "properties": {"source": _STR, "target": _STR}, "required": ["source", "target"]},
    },
    {
        "name": "add_requirement",
        "description": "Add a structured requirement. Use measurable wording for non-functional requirements.",
        "parameters": {"type": "object", "properties": {
            "title": _STR, "description": _STR,
            "kind": {"type": "string", "enum": ["functional", "non_functional"]},
            "category": {**_STR, "description": "For NFRs: performance, scalability, availability, security, privacy, observability, cost, ..."},
            "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            "actors": _STRS, "acceptance_criteria": _STRS,
        }, "required": ["title"]},
    },
    {
        "name": "update_requirement",
        "description": "Change fields of an existing requirement.",
        "parameters": {"type": "object", "properties": {
            "requirement": {**_STR, "description": "Requirement id, e.g. REQ-004."},
            "title": _STR, "description": _STR,
            "kind": {"type": "string", "enum": ["functional", "non_functional"]}, "category": _STR,
            "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            "status": {"type": "string", "enum": ["draft", "review", "approved", "implemented"]},
            "actors": _STRS, "acceptance_criteria": _STRS,
        }, "required": ["requirement"]},
    },
    {
        "name": "remove_requirement",
        "description": "Delete a requirement.",
        "parameters": {"type": "object", "properties": {"requirement": _STR}, "required": ["requirement"]},
    },
    {
        "name": "ask_clarifying_questions",
        "description": "Ask the user questions whose answers are needed to complete the requirements or architecture.",
        "parameters": {"type": "object", "properties": {"questions": _STRS}, "required": ["questions"]},
    },
]

# OpenAI function-calling format (LiteLLM translates it for Gemini and Claude).
TOOLS: list[dict[str, Any]] = [{"type": "function", "function": t} for t in _TOOL_DEFS]

SYSTEM = """You are ArchFlow's architecture co-pilot. You edit a live, structured architecture canvas and \
requirements model that humans are editing at the same time.

You change the model only by calling the provided tools. Make every change needed for the user's request in \
this single response — call as many tools as needed, in order (create a component before connecting it). \
Refer to existing components by their id or exact name. After the tool calls, write one or two sentences \
explaining what you changed and why.

Guidelines:
- Keep edits minimal and targeted; do not rebuild what already exists.
- "Add X between A and B" means: disconnect A→B, add X, connect A→X and X→B.
- Layout: the canvas y axis grows downward. Clients at the top, then gateways, services/agents, then data \
stores. Space components about 320 apart horizontally and 150-160 apart vertically. Place new components near \
the ones they connect to and avoid overlapping existing positions.
- Link components to the requirement IDs they satisfy via requirement_ids.
- When the user describes a product idea, act as a requirements interviewer: add well-formed requirements \
(actors, measurable acceptance criteria, NFRs with numbers), sketch an initial architecture, and use \
ask_clarifying_questions for the most important open questions (at most 4).
- If the request is ambiguous or impossible, change nothing and ask a clarifying question instead."""

ASK_SYSTEM = """You are ArchFlow's project assistant. Answer questions about the user's system using only the \
structured project data provided (requirements, architecture components and connections, version history \
and activity log). Cite requirement IDs and component names. When you review an architecture, be specific \
about failure modes, bottlenecks, security and missing pieces, and suggest concrete changes. Be concise."""


def _tool_to_op(name: str, args: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """Map a model tool call onto an ops.py operation. Returns (op, questions)."""
    pos = {"x": args["x"], "y": args["y"]} if "x" in args and "y" in args else None
    if name == "add_component":
        node = {k: args[k] for k in ("name", "type", "technology", "description", "requirement_ids") if k in args}
        if pos:
            node["position"] = pos
        return {"op": "add_node", "node": node}, []
    if name == "update_component":
        changes = {k: args[k] for k in ("name", "type", "technology", "description", "requirement_ids") if k in args}
        if pos:
            changes["position"] = pos
        return {"op": "update_node", "id": args["component"], "changes": changes}, []
    if name == "remove_component":
        return {"op": "remove_node", "id": args["component"]}, []
    if name == "connect_components":
        return {"op": "add_edge", "edge": {k: args[k] for k in ("source", "target", "protocol", "label") if k in args}}, []
    if name == "disconnect_components":
        return {"op": "remove_edge", "source": args["source"], "target": args["target"]}, []
    if name == "add_requirement":
        return {"op": "add_requirement", "requirement": dict(args)}, []
    if name == "update_requirement":
        changes = {k: v for k, v in args.items() if k != "requirement"}
        return {"op": "update_requirement", "id": args["requirement"], "changes": changes}, []
    if name == "remove_requirement":
        return {"op": "remove_requirement", "id": args["requirement"]}, []
    if name == "ask_clarifying_questions":
        return None, list(args.get("questions") or [])
    return None, []


def _state_for_prompt(state: ProjectState) -> str:
    compact = {
        "components": [{"id": n.id, "name": n.name, "type": n.type, "technology": n.technology,
                        "x": round(n.position.x), "y": round(n.position.y), "requirement_ids": n.requirement_ids,
                        **({"description": n.description} if n.description else {})} for n in state.nodes.values()],
        "connections": [{"source": e.source, "target": e.target, "protocol": e.protocol, **({"label": e.label} if e.label else {})}
                        for e in state.edges.values()],
        "requirements": [r.model_dump(exclude={"rev", "field_revs"}) for r in state.requirements.values()],
    }
    return json.dumps(compact, indent=1)


class AIUnavailable(RuntimeError):
    pass


def _user_prompt(state: ProjectState, text: str, selection: list[str]) -> str:
    selected = [f"{state.nodes[s].name} ({s})" for s in selection if s in state.nodes]
    return (f"<project_state>\n{_state_for_prompt(state)}\n</project_state>\n\n"
            + (f"Currently selected on the canvas: {', '.join(selected)}\n\n" if selected else "")
            + f"Request: {text}")


MAX_ROUNDS = 3


def llm_command(state: ProjectState, text: str, selection: list[str], org_id: str | None = None) -> CommandResult:
    """Turn a request into operations. Thinking models sometimes return only the first step(s) of a plan,
    so each batch is applied to a working copy and the model sees the updated architecture (and any errors)
    until it stops calling tools — at most MAX_ROUNDS model calls."""
    from .ops import ConflictError, OpError, apply_ops

    result = CommandResult(engine="llm")
    working = state
    done: list[str] = []
    feedback = ""
    for _ in range(MAX_ROUNDS):
        prompt = _user_prompt(working, text, selection)
        if done or feedback:
            prompt += ("\n\nChanges already applied for this request (the project state above includes them):\n"
                       + "\n".join(f"- {d}" for d in done)
                       + (f"\n\nThe last batch was rejected: {feedback}. Fix it." if feedback else "")
                       + "\n\nIf anything in the request is still missing, call the tools for the remaining changes only. "
                         "Otherwise call no tools and reply with one or two sentences describing everything you changed.")
        try:
            explanation, calls = llm.complete_tools(SYSTEM, [{"role": "user", "content": prompt}], TOOLS,
                                                    group="reasoning", org_id=org_id, max_tokens=MAX_TOKENS)
        except llm.LLMRefused:
            result.explanation = result.explanation or "The model declined this request."
            return result
        except llm.LLMUnavailable as exc:
            if result.ops:  # keep what was already planned
                break
            raise AIUnavailable(str(exc)) from exc
        if explanation.strip():
            result.explanation = explanation.strip()
        ops = []
        for call in calls:
            op, questions = _tool_to_op(call["name"], call["arguments"])
            if op:
                ops.append(op)
            result.questions += questions
        if not ops:
            break
        try:
            working, summary = apply_ops(working, ops)
        except (OpError, ConflictError) as exc:
            feedback = str(exc)
            continue
        feedback = ""
        result.ops += ops
        done += summary
    if not result.explanation and done:
        result.explanation = "Changed: " + "; ".join(done) + "."
    return result


claude_command = llm_command  # backwards-compatible name


def claude_ask(context: str, question: str, org_id: str | None = None) -> str:
    """Answer a question about the project (name kept for compatibility; uses the "reasoning" group)."""
    messages = [{"role": "user", "content": f"{context}\n\nQuestion: {question}"}]
    try:
        text, _ = llm.complete_tools(ASK_SYSTEM, messages, [], group="reasoning", org_id=org_id,
                                     max_tokens=MAX_TOKENS)
    except llm.LLMRefused:
        return "The model declined to answer this question."
    except llm.LLMUnavailable as exc:
        raise AIUnavailable(str(exc)) from exc
    return text


def run_command(state: ProjectState, text: str, selection: list[str] | None = None) -> CommandResult:
    selection = selection or []
    if MODE != "offline":
        try:
            return llm_command(state, text, selection)
        except AIUnavailable as exc:
            if MODE in ("claude", "llm"):
                raise
            log.info("LLM unavailable (%s); using offline command parser", exc)
    return offline_command(state, text, selection)


# --------------------------------------------------------------------------- offline parser
KEYWORD_TYPES: list[tuple[str, str, str]] = [
    # (regex, node type, default technology)
    (r"redis", "cache", "Redis"), (r"memcache", "cache", "Memcached"), (r"\bcache\b", "cache", "Redis"),
    (r"postgres", "database", "PostgreSQL"), (r"mysql", "database", "MySQL"), (r"dynamo", "database", "DynamoDB"),
    (r"mongo", "database", "MongoDB"), (r"\b(database|db)\b", "database", ""),
    (r"kafka", "queue", "Kafka"), (r"rabbit", "queue", "RabbitMQ"), (r"\bsqs\b", "queue", "SQS"),
    (r"queue|event bus|pub ?sub", "queue", ""),
    (r"\bs3\b|bucket|blob|storage", "storage", ""),
    (r"gateway", "gateway", ""), (r"\bmcp\b", "mcp", "MCP"), (r"agent", "agent", ""),
    (r"\bllm\b|claude|gpt|gemini|model", "llm", ""),
    (r"auth|identity|keycloak|cognito|oauth", "security", ""),
    (r"web|mobile|frontend|\bui\b|client|browser|\bapp\b", "client", ""),
    (r"stripe|twilio|whatsapp|external|third[- ]party|zendesk|jira|github", "external", ""),
]


def infer_type(name: str) -> tuple[str, str]:
    low = name.lower()
    for pattern, ntype, tech in KEYWORD_TYPES:
        if re.search(pattern, low):
            return ntype, tech
    return "service", ""


def _clean(name: str) -> str:
    name = re.sub(r"^(a|an|the|new)\s+", "", name.strip().strip(".\"'"), flags=re.I).strip()
    return name[:1].upper() + name[1:] if name.islower() else name


def _find(state: ProjectState, ref: str):
    ref = _clean(ref)
    node = resolve_node(state, ref)
    if node:
        return node
    low = ref.lower()
    matches = [n for n in state.nodes.values() if low in n.name.lower() or (n.technology and low in n.technology.lower())]
    return matches[0] if len(matches) == 1 else None


def offline_command(state: ProjectState, text: str, selection: list[str]) -> CommandResult:
    res = CommandResult(engine="offline")
    notes: list[str] = []
    for raw in re.split(r"[;\n]+|\.\s+|\bthen\b", text):
        cmd = raw.strip().rstrip(".")
        if not cmd:
            continue
        if not _offline_one(state, cmd, selection, res, notes):
            notes.append(f"Didn't understand “{cmd}”.")
    res.explanation = " ".join(notes)
    if not res.ops and not notes:
        res.explanation = "Nothing to do."
    if any(n.startswith("Didn't") for n in notes):
        res.explanation += (" Offline mode understands: add X [between A and B | connected to A], remove X, "
                            "connect A to B [via P], disconnect A from B, rename A to B, change A to B, "
                            "move A below/above/left of/right of B. Configure GEMINI_API_KEY (or ANTHROPIC_API_KEY / OPENAI_API_KEY) for full AI.")
    return res


def _offline_one(state: ProjectState, cmd: str, selection: list[str], res: CommandResult, notes: list[str]) -> bool:
    m = re.match(r"(?:add|insert|put)\s+(.+?)\s+between\s+(.+?)\s+and\s+(.+)$", cmd, re.I)
    if m:
        new, a, b = _clean(m.group(1)), _find(state, m.group(2)), _find(state, m.group(3))
        if not a or not b:
            notes.append(f"Couldn't find '{m.group(2) if not a else m.group(3)}'.")
            return True
        ntype, tech = infer_type(new)
        pos = {"x": (a.position.x + b.position.x) / 2 + 40, "y": (a.position.y + b.position.y) / 2}
        if any(e.source == a.id and e.target == b.id for e in state.edges.values()):
            res.ops.append({"op": "remove_edge", "source": a.id, "target": b.id})
        res.ops += [
            {"op": "add_node", "node": {"name": new, "type": ntype, "technology": tech, "position": pos}},
            {"op": "add_edge", "edge": {"source": a.id, "target": new}},
            {"op": "add_edge", "edge": {"source": new, "target": b.id}},
        ]
        notes.append(f"Inserted {new} between {a.name} and {b.name}.")
        return True

    m = re.match(r"(?:add|create)\s+(.+?)(?:\s+(?:connected to|connecting to|linked to|for|to|behind|in front of)\s+(.+))?$", cmd, re.I)
    if m:
        new = _clean(m.group(1))
        anchor = _find(state, m.group(2)) if m.group(2) else (state.nodes.get(selection[0]) if selection else None)
        ntype, tech = infer_type(new)
        node: dict[str, Any] = {"name": new, "type": ntype, "technology": tech}
        if anchor:
            node["position"] = {"x": anchor.position.x + 320, "y": anchor.position.y + 40}
        res.ops.append({"op": "add_node", "node": node})
        if anchor:
            res.ops.append({"op": "add_edge", "edge": {"source": anchor.id, "target": new}})
        notes.append(f"Added {new}" + (f" connected to {anchor.name}." if anchor else "."))
        return True

    m = re.match(r"(?:remove|delete|drop)\s+(.+)$", cmd, re.I)
    if m:
        node = _find(state, m.group(1))
        if not node:
            notes.append(f"Couldn't find '{m.group(1)}'.")
            return True
        res.ops.append({"op": "remove_node", "id": node.id})
        notes.append(f"Removed {node.name}.")
        return True

    m = re.match(r"(?:connect|link)\s+(.+?)\s+(?:to|with|and)\s+(.+?)(?:\s+(?:via|using|over)\s+(.+))?$", cmd, re.I)
    if m:
        a, b = _find(state, m.group(1)), _find(state, m.group(2))
        if not a or not b:
            notes.append(f"Couldn't find '{m.group(1) if not a else m.group(2)}'.")
            return True
        res.ops.append({"op": "add_edge", "edge": {"source": a.id, "target": b.id, "protocol": (m.group(3) or "").strip()}})
        notes.append(f"Connected {a.name} → {b.name}.")
        return True

    m = re.match(r"(?:disconnect|unlink)\s+(.+?)\s+(?:from|and)\s+(.+)$", cmd, re.I)
    if m:
        a, b = _find(state, m.group(1)), _find(state, m.group(2))
        if not a or not b:
            notes.append("Couldn't find one of those components.")
            return True
        for s, t in ((a, b), (b, a)):
            if any(e.source == s.id and e.target == t.id for e in state.edges.values()):
                res.ops.append({"op": "remove_edge", "source": s.id, "target": t.id})
        notes.append(f"Disconnected {a.name} and {b.name}.")
        return True

    m = re.match(r"rename\s+(.+?)\s+to\s+(.+)$", cmd, re.I)
    if m:
        node = _find(state, m.group(1))
        if not node:
            notes.append(f"Couldn't find '{m.group(1)}'.")
            return True
        res.ops.append({"op": "update_node", "id": node.id, "changes": {"name": _clean(m.group(2))}})
        notes.append(f"Renamed {node.name}.")
        return True

    m = re.match(r"(?:change|replace|switch|convert)\s+(.+?)\s+(?:to|with|into)\s+(.+)$", cmd, re.I)
    if m:
        node = _find(state, m.group(1))
        if not node:
            notes.append(f"Couldn't find '{m.group(1)}'.")
            return True
        new = _clean(m.group(2))
        ntype, tech = infer_type(new)
        changes: dict[str, Any] = {"technology": tech or new}
        if node.technology and node.technology.lower() in node.name.lower():
            changes["name"] = node.name.replace(node.technology, tech or new)
        if ntype != "service" and ntype != node.type:
            changes["type"] = ntype
        res.ops.append({"op": "update_node", "id": node.id, "changes": changes})
        notes.append(f"Changed {node.name} to {tech or new}.")
        return True

    m = re.match(r"move\s+(.+?)\s+(below|under|above|over|left of|right of|next to)\s+(.+)$", cmd, re.I)
    if m:
        a, b = _find(state, m.group(1)), _find(state, m.group(3))
        if not a or not b:
            notes.append("Couldn't find one of those components.")
            return True
        dx, dy = {"below": (0, 160), "under": (0, 160), "above": (0, -160), "over": (0, -160),
                  "left of": (-320, 0), "right of": (320, 0), "next to": (320, 0)}[m.group(2).lower()]
        res.ops.append({"op": "update_node", "id": a.id,
                        "changes": {"position": {"x": b.position.x + dx, "y": b.position.y + dy}}})
        notes.append(f"Moved {a.name} {m.group(2)} {b.name}.")
        return True
    return False
