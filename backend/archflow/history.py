"""Requirement history: a version history per requirement with field-level diffs.

Every place a branch's state changes (``main.commit_ops``, the collab projection, restore, adopting a decision)
calls :func:`record` with the state before and after. Comparing the two states, rather than the operations,
catches every path alike: canvas edits that arrive as a Yjs projection, AI and MCP operations, restores.
Rows live in ``requirement_revisions`` (one per requirement per change) and are read by
``GET /api/projects/{pid}/requirements/{rid}/history``.

Also here: ``DELETE /api/projects/{pid}/requirements/{rid}``, the admin-only permanent removal of a
soft-deleted requirement (the ``purge_requirement`` op).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException

from .models import ProjectState, Requirement

BOOKKEEPING = {"rev", "field_revs"}
# Diffed fields, in display order. ``deleted`` is the action itself (deleted / restored), not a field change.
FIELDS = [f for f in Requirement.model_fields if f not in BOOKKEEPING | {"id", "deleted"}]


def requirement_changes(old: ProjectState, new: ProjectState) -> list[dict[str, Any]]:
    """What happened to each requirement between two states: [{requirement_id, action, changes}], where
    action is added | updated | deleted | restored | purged and changes is [{field, before, after}]."""
    out: list[dict[str, Any]] = []
    for rid in sorted(old.requirements.keys() | new.requirements.keys()):
        a, b = old.requirements.get(rid), new.requirements.get(rid)
        before = a.model_dump(exclude=BOOKKEEPING) if a else {}
        after = b.model_dump(exclude=BOOKKEEPING) if b else {}
        if a is None:
            action, changes = "added", [{"field": f, "before": None, "after": after[f]} for f in FIELDS if after[f]]
        elif b is None:
            action, changes = "purged", []
        else:
            changes = [{"field": f, "before": before[f], "after": after[f]} for f in FIELDS if before[f] != after[f]]
            if a.deleted != b.deleted:
                action = "deleted" if b.deleted else "restored"
            elif changes:
                action = "updated"
            else:
                continue
        out.append({"requirement_id": rid, "action": action, "changes": changes})
    return out


def record(store: Any, pid: str, branch: str, actor: str, old: ProjectState, new: ProjectState) -> None:
    """Log the requirement changes between ``old`` and ``new`` (call inside the branch transaction)."""
    store.log_requirement_revisions(pid, branch, actor, new.rev, requirement_changes(old, new))


def actor_kind(actor: str) -> str:
    """Who made a change, from the actor label the API writes: ai (assistant, design chat, generated design),
    agent (an MCP client) or human."""
    if actor.startswith("AI (") or actor.endswith(("(design chat)", "(low-level design)")):
        return "ai"
    if actor.endswith(" via MCP"):
        return "agent"
    return "human"


def register_routes(app: FastAPI, store: Any, *, authz: Any, state_or_404: Callable[[str, str], ProjectState],
                    commit_ops: Callable[..., Awaitable[tuple[ProjectState, list[str]]]]) -> None:
    read, admin = authz.project("read"), authz.project("admin")

    @app.get("/api/projects/{pid}/requirements/{rid}/history")
    def requirement_history(pid: str, rid: str, branch: str = "main", limit: int = 200, a=Depends(read)):
        """The requirement's changes, newest first: who, when, AI or human, and each field before → after."""
        state = state_or_404(pid, branch)
        rows = store.list_requirement_revisions(pid, branch, rid, min(max(limit, 1), 500))
        if rid not in state.requirements and not rows:
            raise HTTPException(404, "requirement not found")
        return [{**r, "actor_kind": actor_kind(r["actor"])} for r in rows]

    @app.delete("/api/projects/{pid}/requirements/{rid}")
    async def purge_requirement(pid: str, rid: str, branch: str = "main", a=Depends(admin)):
        """Remove a deleted requirement permanently, with its links (admins only; its history is kept)."""
        state = state_or_404(pid, branch)
        if rid not in state.requirements:
            raise HTTPException(404, "requirement not found")
        new_state, summary = await commit_ops(pid, branch, [{"op": "purge_requirement", "id": rid}], a.actor)
        return {"summary": summary, "rev": new_state.rev}
