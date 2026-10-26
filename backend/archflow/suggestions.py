"""Suggested next steps from the LLM: starting prompts and follow-ups for Edit, Ask and the design chat.

A Pydantic AI agent (prompt ``assistant.suggestions``) reads the project (components, connections, requirements
and whether they are traced, open health issues), its memory, the canvas selection and the last exchange, and
returns 3-4 short prompts grounded in that data. Results are cached per project state so re-rendering the chat
costs nothing. With no AI configured the endpoint returns ``engine: "none"`` and the client shows its offline
suggestions instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import agents, conversations, llm
from .analysis import full_analysis
from .models import ProjectState

log = logging.getLogger("archflow.suggestions")

PROMPT = "assistant.suggestions"
MODES = ("edit", "ask", "design")
MAX_SUGGESTIONS = 4
CACHE_SIZE = 256
CACHE_SECONDS = 600

SCHEMA: dict[str, Any] = {"type": "object", "properties": {"suggestions": {"type": "array", "items": {
    "type": "object", "properties": {"label": {"type": "string"}, "prompt": {"type": "string"},
                                     "why": {"type": "string"},
                                     "kind": {"type": "string", "enum": ["start", "follow_up", "reply"]}},
    "required": ["label", "prompt"]}}}, "required": ["suggestions"]}


class SuggestIn(BaseModel):
    mode: str = Field("edit", pattern="^(edit|ask|design)$")
    selection: list[str] = Field([], max_length=50)
    conversation_id: str | None = None
    last_question: str = Field("", max_length=8000)
    last_answer: str = Field("", max_length=20000)


def project_summary(state: ProjectState, limit: int = 60) -> dict[str, Any]:
    """What the suggestion agent needs to know, compactly: top-level components, connections, requirements and
    whether each is traced to a component, and the open health issues."""
    analysis = full_analysis(state)
    top = [n for n in state.nodes.values() if not n.parent_id and n.type != "note"]
    names = {n.id: n.name for n in top}
    untraced = set(analysis["traceability"].get("untraced_requirements") or [])
    issues = sorted(analysis["architecture_issues"] + analysis["requirement_issues"],
                    key=lambda i: {"error": 0, "warning": 1}.get(i.get("severity"), 2))
    return {
        "components": [{"name": n.name, "type": n.type, **({"technology": n.technology} if n.technology else {})}
                       for n in top[:limit]],
        "connections": [f"{names[e.source]} -> {names[e.target]}" + (f" ({e.protocol})" if e.protocol else "")
                        for e in state.edges.values() if e.source in names and e.target in names][: limit * 2],
        "requirements": [{"id": r.id, "title": r.title, "kind": r.kind, "priority": r.priority,
                          **({"category": r.category} if r.category else {}), "traced": r.id not in untraced}
                         for r in state.requirements.values()][:limit],
        "health": {k: analysis["health"][k] for k in ("architecture_score", "traceability", "issues")},
        "open_issues": [f"[{i.get('severity')}] {i.get('message')}" for i in issues
                        if i.get("severity") in ("error", "warning")][:8],
    }


def clean(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    out, seen = [], set()
    for s in items:
        label = " ".join(str(s.get("label") or "").split())[:90]
        prompt = " ".join(str(s.get("prompt") or "").split())[:600] or label
        key = label.lower()
        if not label or key in seen:
            continue
        seen.add(key)
        kind = s.get("kind") if s.get("kind") in ("start", "follow_up", "reply") else "follow_up"
        out.append({"label": label, "prompt": prompt, "why": " ".join(str(s.get("why") or "").split())[:160],
                    "kind": kind})
    return out[:MAX_SUGGESTIONS]


def suggest(state: ProjectState, mode: str, *, selection: list[str] | None = None, memory: str = "",
            recent: str = "", last_question: str = "", last_answer: str = "", org_id: str | None = None,
            version: str | None = None) -> list[dict[str, str]]:
    """3-4 suggestions for ``mode`` (edit | ask | design). Raises llm.LLMUnavailable without a model."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    selected = [f"{state.nodes[s].name} ({state.nodes[s].type})" for s in selection or [] if s in state.nodes]
    last = ""
    if last_question or last_answer:
        last = f"User: {last_question.strip()[:3000]}\nArchFlow: {last_answer.strip()[:4000]}"
    asked = (f"Follow-ups to: {last_question.strip()[:200]}" if last_question.strip()
             else f"Suggest {'edits' if mode == 'edit' else 'questions' if mode == 'ask' else 'design ideas'}")
    data = agents.run_json(PROMPT, SCHEMA, org_id=org_id, version=version, question=asked, variables={
        "mode": mode, "project_json": json.dumps(project_summary(state), indent=1), "memory": memory,
        "selection": ", ".join(selected), "recent": recent, "last_exchange": last})
    return clean(data.get("suggestions") or [])


class _Cache:
    def __init__(self) -> None:
        self._data: OrderedDict[str, tuple[float, list[dict[str, str]]]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> list[dict[str, str]] | None:
        with self._lock:
            hit = self._data.get(key)
            if not hit or time.monotonic() - hit[0] > CACHE_SECONDS:
                return None
            self._data.move_to_end(key)
            return hit[1]

    def put(self, key: str, value: list[dict[str, str]]) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            self._data.move_to_end(key)
            while len(self._data) > CACHE_SIZE:
                self._data.popitem(last=False)


def register_routes(app: FastAPI, store: Any, *, authz: Any,
                    state_or_404: Callable[[str, str], ProjectState]) -> None:
    read = authz.project("read")
    cache = _Cache()

    @app.post("/api/projects/{pid}/ai/suggestions")
    async def ai_suggestions(pid: str, body: SuggestIn, branch: str = "main", a=Depends(read)):
        state = state_or_404(pid, branch)
        if not llm.available(a.org_id):
            return {"suggestions": [], "engine": "none"}
        recent = ""
        if body.conversation_id:
            conv = store.get_conversation(body.conversation_id)
            if not conv or conv["project_id"] != pid:
                raise HTTPException(404, "conversation not found")
            recent = conversations.transcript(store.list_messages(conv["id"])[-4:])
        memory = conversations.memory_block(store, pid)
        key = hashlib.sha256(json.dumps([pid, branch, state.rev, body.mode, sorted(body.selection), recent,
                                         body.last_question, body.last_answer, memory]).encode()).hexdigest()
        hit = cache.get(key)
        if hit is not None:
            return {"suggestions": hit, "engine": "llm", "cached": True}
        try:
            items = await asyncio.to_thread(suggest, state, body.mode, selection=body.selection, memory=memory,
                                            recent=recent, last_question=body.last_question,
                                            last_answer=body.last_answer, org_id=a.org_id)
        except (llm.LLMUnavailable, llm.LLMRefused) as exc:
            log.info("suggestions unavailable: %s", type(exc).__name__)
            return {"suggestions": [], "engine": "none"}
        cache.put(key, items)
        return {"suggestions": items, "engine": "llm"}
