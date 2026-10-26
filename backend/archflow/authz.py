"""Authorization: org roles, FastAPI dependencies and CSRF protection.

Roles (org-level, highest first): owner > admin > architect > developer > reviewer > viewer.

=========  =======================================================================================
action     minimum role and what it covers
=========  =======================================================================================
read       viewer     — view projects, versions, diffs, analysis, decisions, members; collab read-only
review     reviewer   — read + change a decision's *status* (accept / reject / supersede)
edit       developer  — canvas, requirements, versions, branches, AI commands, design chat, convert,
                        low-level design, decisions (architect is a label for now: same rights as developer)
admin      admin      — members, invites, API tokens, AI provider keys, delete projects
owner      owner      — org settings (rename); granting or removing the owner role
=========  =======================================================================================

Errors: 401 when not signed in, 404 when the project/org is not in one of your orgs (no existence leak),
403 when you are a member but your role is insufficient.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from . import auth, llm, users

ROLES = ("viewer", "reviewer", "developer", "architect", "admin", "owner")
RANK = {r: i for i, r in enumerate(ROLES)}
ACTIONS = {"read": "viewer", "review": "reviewer", "edit": "developer", "admin": "admin", "owner": "owner"}


def can(role: str | None, action: str) -> bool:
    if role not in RANK:
        return False
    return RANK[role] >= RANK[ACTIONS[action]]


def role_scopes(role: str | None) -> list[str]:
    """The MCP scopes a role may hold: viewers and reviewers are read-only."""
    if can(role, "edit"):
        return [auth.SCOPE_READ, auth.SCOPE_WRITE]
    return [auth.SCOPE_READ] if role in RANK else []


def cap_scopes(role: str | None, requested: list[str]) -> list[str]:
    allowed = set(role_scopes(role))
    return [s for s in auth.normalize_scopes(requested) if s in allowed]


@dataclass
class Access:
    """The authenticated caller and, for project/org routes, their role there."""
    user: dict[str, Any]
    role: str | None = None
    org_id: str | None = None
    project: dict[str, Any] | None = None

    @property
    def actor(self) -> str:
        return self.user["name"]

    def can(self, action: str) -> bool:
        return can(self.role, action)


class Authz:
    """Builds the FastAPI dependencies for one store."""

    def __init__(self, store):
        self.store = store

    def user_from_token(self, token: str | None) -> dict[str, Any] | None:
        return users.resolve_session(self.store, token)

    def session_user(self, request: Request) -> dict[str, Any] | None:
        cached = getattr(request.state, "archflow_user", None)
        if cached is not None:
            return cached or None
        user = self.user_from_token(request.cookies.get(users.SESSION_COOKIE))
        request.state.archflow_user = user or {}
        return user

    # ---- dependencies ----
    def current_user(self, request: Request) -> dict[str, Any]:
        user = self.session_user(request)
        if not user:
            raise HTTPException(401, "Not signed in")
        return user

    def check_org(self, user: dict[str, Any], org_id: str, action: str) -> Access:
        role = self.store.get_role(org_id, user["id"])
        if role is None or not self.store.get_org(org_id):
            raise HTTPException(404, "org not found")
        if not can(role, action):
            raise HTTPException(403, f"Your role ({role}) cannot do this; it needs {ACTIONS[action]} or above.")
        return Access(user=user, role=role, org_id=org_id)

    def check_project(self, user: dict[str, Any], pid: str, action: str) -> Access:
        project = self.store.get_project(pid)
        role = self.store.get_role(project["org_id"], user["id"]) if project else None
        if not project or role is None:
            raise HTTPException(404, "project not found")
        if not can(role, action):
            raise HTTPException(403, f"Your role ({role}) cannot do this; it needs {ACTIONS[action]} or above.")
        return Access(user=user, role=role, org_id=project["org_id"], project=project)

    def project(self, action: str) -> Callable[..., Awaitable[Access]]:
        """Dependency for routes with a ``{pid}`` path parameter. Async so that it can attribute the request's AI
        calls (usage rows and traces) to the person: the store lookups still run in a worker thread."""
        async def dep(pid: str, request: Request) -> Access:
            a = await run_in_threadpool(lambda: self.check_project(self.current_user(request), pid, action))
            attribute(a)
            return a
        return dep

    def org(self, action: str) -> Callable[..., Awaitable[Access]]:
        """Dependency for routes with an ``{org_id}`` path parameter (attributes AI calls like ``project``)."""
        async def dep(org_id: str, request: Request) -> Access:
            a = await run_in_threadpool(lambda: self.check_org(self.current_user(request), org_id, action))
            attribute(a)
            return a
        return dep


def attribute(a: Access) -> None:
    """Attribute the AI calls of the current request to this person (see llm.usage_scope, agents.tracing)."""
    llm.set_usage_scope(user_id=a.user.get("id", ""), user_name=a.user.get("name", ""))


# --------------------------------------------------------------------------- CSRF
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
# Not cookie-authenticated: shared secret (collab server) or OAuth / bearer-token endpoints used by MCP clients.
CSRF_EXEMPT_PREFIXES = ("/api/internal/", "/token", "/register", "/revoke", "/mcp")
# Always checked, even without a session cookie (login CSRF).
CSRF_ALWAYS_PREFIXES = ("/api/auth/", "/oauth/")


class CSRFMiddleware:
    """For unsafe requests that carry the session cookie (and for login/signup), require the browser's Origin
    (or, failing that, Referer) to be one of the trusted origins. Pure ASGI so streaming responses pass through."""

    def __init__(self, app, allowed: list[str] | None = None):
        self.app = app
        self.allowed = set(allowed if allowed is not None else users.allowed_origins())

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            path: str = scope.get("path", "")
            if not path.startswith(CSRF_EXEMPT_PREFIXES):
                headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
                has_cookie = f"{users.SESSION_COOKIE}=" in headers.get("cookie", "")
                if has_cookie or path.startswith(CSRF_ALWAYS_PREFIXES):
                    origin = headers.get("origin")
                    origin = users.origin_of(origin) if origin and origin != "null" else users.origin_of(headers.get("referer"))
                    if origin not in self.allowed:
                        body = json.dumps({"detail": "Cross-site request blocked (origin not allowed)"}).encode()
                        await send({"type": "http.response.start", "status": 403,
                                    "headers": [(b"content-type", b"application/json"),
                                                (b"content-length", str(len(body)).encode())]})
                        await send({"type": "http.response.body", "body": body})
                        return
        await self.app(scope, receive, send)
