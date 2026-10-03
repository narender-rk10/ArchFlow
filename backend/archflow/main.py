"""ArchFlow HTTP + WebSocket API.

Run:  uvicorn archflow.main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import ai, auth, catalog, chat, collab_sync, lld
from .analysis import full_analysis, trace_requirement
from .diff import diff_states, diff_to_text
from .models import ProjectState
from .ops import ConflictError, OpError, apply_ops, resolve_node
from .store import Store, default_store
from .templates import TEMPLATES, build_template
from .mcp_remote import Services, build_remote_mcp
from .vibe import TARGETS, build_prompt


# --------------------------------------------------------------------------- request bodies
class ProjectIn(BaseModel):
    name: str
    description: str = ""
    template: str = "blank"
    org_id: str = "org-default"


class OrgIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    owner: str = ""


class MemberIn(BaseModel):
    user_name: str = Field(min_length=1, max_length=80)
    role: str = Field("member", pattern="^(owner|admin|architect|developer|reviewer|viewer|member)$")


class OpsIn(BaseModel):
    ops: list[dict[str, Any]]
    actor: str = "anonymous"


class CommitIn(BaseModel):
    message: str
    author: str = "anonymous"


class RestoreIn(BaseModel):
    version: int
    actor: str = "anonymous"


class BranchIn(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9._/-]{1,60}$")
    from_version: int | None = None
    from_branch: str = "main"


class PromptIn(BaseModel):
    node_ids: list[str] = []
    requirement_ids: list[str] = []
    target: str = "claude-code"
    extra: str = ""


class CommandIn(BaseModel):
    text: str
    selection: list[str] = []
    actor: str = "anonymous"


class ProjectionIn(BaseModel):
    state: dict[str, Any]
    actors: list[str] = []


class ConvertIn(BaseModel):
    target: str = Field(pattern="^(aws|azure|gcp|oss)$")
    actor: str = "anonymous"


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    user_name: str = Field(min_length=1, max_length=80)
    access: str = Field("write", pattern="^(read|write)$")
    expires_in_days: int | None = Field(None, ge=1, le=3650)


class ChatIn(BaseModel):
    turns: list[dict[str, Any]]
    actor: str = "anonymous"


class ChatApplyIn(BaseModel):
    option: dict[str, Any]
    requirements: list[dict[str, Any]] = []
    mode: str = Field("replace", pattern="^(replace|merge|branch)$")
    branch_name: str | None = Field(None, pattern=r"^[A-Za-z0-9._/-]{1,60}$")
    actor: str = "anonymous"


class AskIn(BaseModel):
    question: str


class LLDIn(BaseModel):
    actor: str = "anonymous"
    replace: bool = False


# --------------------------------------------------------------------------- realtime hub
class Hub:
    """Fan-out of state changes and presence for each (project, branch) room."""

    def __init__(self) -> None:
        self.rooms: dict[str, dict[WebSocket, dict[str, Any]]] = {}

    @staticmethod
    def key(pid: str, branch: str) -> str:
        return f"{pid}:{branch}"

    async def join(self, room: str, ws: WebSocket, user: dict[str, Any]) -> None:
        self.rooms.setdefault(room, {})[ws] = user
        await self.broadcast_presence(room)

    async def leave(self, room: str, ws: WebSocket) -> None:
        self.rooms.get(room, {}).pop(ws, None)
        await self.broadcast(room, {"type": "left", "client_id": id(ws)})
        await self.broadcast_presence(room)

    async def broadcast(self, room: str, msg: dict[str, Any], exclude: WebSocket | None = None) -> None:
        data = json.dumps(msg, default=str)
        for ws in list(self.rooms.get(room, {})):
            if ws is exclude:
                continue
            try:
                await ws.send_text(data)
            except Exception:
                self.rooms.get(room, {}).pop(ws, None)

    async def broadcast_presence(self, room: str) -> None:
        users = [{**u, "client_id": id(ws)} for ws, u in self.rooms.get(room, {}).items()]
        await self.broadcast(room, {"type": "presence", "users": users})


# --------------------------------------------------------------------------- app
def create_app(store: Store | None = None) -> FastAPI:
    store = store or default_store()
    hub = Hub()
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The remote MCP endpoint's session manager must run for the lifetime of the app.
        async with app.state.mcp.session_manager.run():
            yield

    app = FastAPI(title="ArchFlow", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.state.store = store
    app.state.hub = hub

    def project_or_404(pid: str) -> dict[str, Any]:
        project = store.get_project(pid)
        if not project:
            raise HTTPException(404, "project not found")
        return project

    def state_or_404(pid: str, branch: str) -> ProjectState:
        project_or_404(pid)
        state = store.get_state(pid, branch)
        if state is None:
            raise HTTPException(404, f"branch '{branch}' not found")
        return state

    async def sync_to_live_doc(pid: str, branch: str, old: ProjectState, new: ProjectState, actor: str) -> None:
        """Push a server-side change into the live collaborative document (field-level, merges with humans)."""
        if not await collab_sync.push_patch(pid, branch, collab_sync.state_patch(old, new), actor):
            # Collab server unreachable: drop the persisted document so it is rebuilt from this state.
            store.delete_ydoc(pid, branch)

    async def commit_ops(pid: str, branch: str, ops: list[dict[str, Any]], actor: str) -> tuple[ProjectState, list[str]]:
        """The one place state changes: apply atomically, persist, log, broadcast."""
        project_or_404(pid)
        with store.transaction(pid, branch) as state:  # row lock: safe across server processes
            if state is None:
                raise HTTPException(404, f"branch '{branch}' not found")
            new_state, summary = apply_ops(state, ops)
            if new_state is not state:
                store.save_state(pid, branch, new_state)
                store.log_activity(pid, branch, actor, summary, new_state.rev)
        if new_state is not state:
            await sync_to_live_doc(pid, branch, state, new_state, actor)
        if summary or new_state.rev != state.rev:
            await hub.broadcast(Hub.key(pid, branch), {
                "type": "state", "state": new_state.model_dump(), "actor": actor, "summary": summary})
        return new_state, summary

    # ---- errors ----
    @app.exception_handler(ConflictError)
    async def _conflict(_, exc: ConflictError):
        return JSONResponse(status_code=409, content={"detail": "conflict", "conflicts": exc.conflicts})

    @app.exception_handler(OpError)
    async def _op_error(_, exc: OpError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    # ---- meta ----
    @app.get("/api/health")
    def api_health():
        from . import llm
        return {"ok": True, "ai_mode": ai.MODE, "model": ai.MODEL, "llm": llm.status()}

    @app.get("/api/catalog")
    def get_catalog():
        return catalog.catalog_payload()

    @app.get("/api/templates")
    def list_templates():
        return [{"key": k, "name": v[0]} for k, v in TEMPLATES.items()]

    # ---- projects ----
    @app.get("/api/projects")
    def list_projects(org_id: str | None = None):
        return store.list_projects(org_id)

    # ---- organizations (TODO: real authentication; users are display names for now) ----
    @app.get("/api/orgs")
    def list_orgs():
        return store.list_orgs()

    @app.post("/api/orgs", status_code=201)
    def create_org(body: OrgIn):
        org = store.create_org(body.name)
        if body.owner:
            store.add_member(org["id"], body.owner, "owner")
        return org

    @app.get("/api/orgs/{org_id}/members")
    def list_members(org_id: str):
        if not store.get_org(org_id):
            raise HTTPException(404, "org not found")
        return store.list_members(org_id)

    @app.post("/api/orgs/{org_id}/members", status_code=201)
    def add_member(org_id: str, body: MemberIn):
        if not store.get_org(org_id):
            raise HTTPException(404, "org not found")
        store.add_member(org_id, body.user_name, body.role)
        return store.list_members(org_id)

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectIn):
        if not store.get_org(body.org_id):
            raise HTTPException(404, "org not found")
        project = store.create_project(body.name, body.description, build_template(body.template), org_id=body.org_id)
        state = store.get_state(project["id"], "main")
        if state.nodes or state.requirements:
            store.commit(project["id"], "main", f"Created from template '{body.template}'", "ArchFlow", state)
        return project

    @app.delete("/api/projects/{pid}", status_code=204)
    def delete_project(pid: str):
        project_or_404(pid)
        store.delete_project(pid)

    @app.get("/api/projects/{pid}")
    def get_project(pid: str, branch: str = "main"):
        project = project_or_404(pid)
        state = state_or_404(pid, branch)
        return {"project": project, "branch": branch, "branches": store.list_branches(pid),
                "head_version": store.head_version(pid, branch), "state": state.model_dump()}

    # ---- operations ----
    @app.post("/api/projects/{pid}/ops")
    async def post_ops(pid: str, body: OpsIn, branch: str = "main"):
        state, summary = await commit_ops(pid, branch, body.ops, body.actor)
        return {"state": state.model_dump(), "summary": summary}

    @app.get("/api/projects/{pid}/activity")
    def activity(pid: str, branch: str = "main", limit: int = 100):
        project_or_404(pid)
        return store.list_activity(pid, branch, limit)

    # ---- versions & branches ----
    async def commit_version(pid: str, branch: str, message: str, author: str) -> dict[str, Any]:
        state = state_or_404(pid, branch)
        version = store.commit(pid, branch, message, author, state)
        await hub.broadcast(Hub.key(pid, branch), {"type": "versions", "actor": author})
        return version

    @app.post("/api/projects/{pid}/commit", status_code=201)
    async def commit(pid: str, body: CommitIn, branch: str = "main"):
        return await commit_version(pid, branch, body.message, body.author)

    @app.get("/api/projects/{pid}/versions")
    def versions(pid: str):
        project_or_404(pid)
        return store.list_versions(pid)

    def resolve_ref(pid: str, branch: str, ref: str) -> ProjectState:
        """'working' = branch working state; 'N' or 'vN' = version N; 'branch:<name>' = another branch."""
        if ref == "working":
            return state_or_404(pid, branch)
        if ref.startswith("branch:"):
            return state_or_404(pid, ref.split(":", 1)[1])
        m = re.fullmatch(r"v?(\d+)", ref)
        if not m:
            raise HTTPException(400, f"bad version reference '{ref}'")
        v = store.get_version(pid, int(m.group(1)))
        if not v:
            raise HTTPException(404, f"version {ref} not found")
        return v["state"]

    @app.get("/api/projects/{pid}/diff")
    def diff(pid: str, from_: str = Query(alias="from"), to: str = "working", branch: str = "main"):
        project_or_404(pid)
        old, new = resolve_ref(pid, branch, from_), resolve_ref(pid, branch, to)
        d = diff_states(old, new)
        return {**d, "text": diff_to_text(d), "from_state": old.model_dump()}

    @app.post("/api/projects/{pid}/restore")
    async def restore(pid: str, body: RestoreIn, branch: str = "main"):
        v = store.get_version(pid, body.version)
        if not v:
            raise HTTPException(404, "version not found")
        with store.transaction(pid, branch) as current:
            if current is None:
                raise HTTPException(404, f"branch '{branch}' not found")
            restored = v["state"].model_copy(deep=True)
            restored.rev = current.rev + 1  # keep revisions monotonic so conflict detection stays valid
            for coll in (restored.nodes, restored.edges, restored.requirements):
                for item in coll.values():
                    item.rev = restored.rev
                    item.field_revs = {f: restored.rev for f in item.field_revs}
            store.save_state(pid, branch, restored)
            store.log_activity(pid, branch, body.actor, [f"restored v{body.version}"], restored.rev)
        await sync_to_live_doc(pid, branch, current, restored, body.actor)
        await hub.broadcast(Hub.key(pid, branch), {"type": "state", "state": restored.model_dump(),
                                                   "actor": body.actor, "summary": [f"restored v{body.version}"]})
        return {"state": restored.model_dump()}

    @app.post("/api/projects/{pid}/branches", status_code=201)
    def create_branch(pid: str, body: BranchIn):
        project_or_404(pid)
        if store.get_state(pid, body.name) is not None:
            raise HTTPException(409, "branch already exists")
        if body.from_version is not None:
            v = store.get_version(pid, body.from_version)
            if not v:
                raise HTTPException(404, "version not found")
            state, base = v["state"], body.from_version
        else:
            state, base = state_or_404(pid, body.from_branch), store.head_version(pid, body.from_branch)
        store.create_branch(pid, body.name, state, base)
        return {"name": body.name, "base_version": base}

    # ---- analysis ----
    @app.get("/api/projects/{pid}/analysis")
    def analysis(pid: str, branch: str = "main"):
        return full_analysis(state_or_404(pid, branch))

    @app.get("/api/projects/{pid}/trace/{req_id}")
    def trace(pid: str, req_id: str, branch: str = "main"):
        state = state_or_404(pid, branch)
        if req_id not in state.requirements:
            raise HTTPException(404, "requirement not found")
        return trace_requirement(state, req_id)

    # ---- HLD → LLD drill-down ----
    def node_or_404(state: ProjectState, node_id: str):
        node = resolve_node(state, node_id)
        if not node:
            raise HTTPException(404, "component not found")
        return node

    @app.post("/api/projects/{pid}/nodes/{node_id}/lld")
    async def generate_lld(pid: str, node_id: str, body: LLDIn, branch: str = "main"):
        """Generate the component's internal (low-level) diagram; applied as ordinary ops, so it syncs live."""
        state = state_or_404(pid, branch)
        node = node_or_404(state, node_id)
        try:
            ops, info = await asyncio.to_thread(lld.build, state, node.id, body.replace,
                                                project_or_404(pid).get("org_id"))
        except lld.LLDError as exc:
            raise HTTPException(409, str(exc)) from exc
        new_state, summary = await commit_ops(pid, branch, ops, f"{body.actor} (low-level design)")
        return {**info, "summary": summary, "rev": new_state.rev}

    @app.get("/api/projects/{pid}/nodes/{node_id}/views")
    def node_views(pid: str, node_id: str, branch: str = "main"):
        """Read-only views derived from the model: API list, ER data, Mermaid sequence and ER diagrams."""
        state = state_or_404(pid, branch)
        return lld.views(state, node_or_404(state, node_id))

    # ---- vibe coding ----
    @app.get("/api/prompt-targets")
    def prompt_targets():
        return [{"key": k, "name": v} for k, v in TARGETS.items()]

    @app.post("/api/projects/{pid}/prompt")
    def prompt(pid: str, body: PromptIn, branch: str = "main"):
        project = project_or_404(pid)
        state = state_or_404(pid, branch)
        return {"prompt": build_prompt(state, project["name"], body.node_ids, body.requirement_ids, body.target, body.extra)}

    # ---- AI ----
    async def run_ai_command(pid: str, branch: str, text: str, selection: list[str], actor: str) -> dict[str, Any]:
        state = state_or_404(pid, branch)
        room = Hub.key(pid, branch)
        await hub.broadcast(room, {"type": "ai_status", "status": "thinking", "actor": actor, "text": text})
        try:
            result = await asyncio.to_thread(ai.run_command, state, text, selection)
        except ai.AIUnavailable as exc:
            await hub.broadcast(room, {"type": "ai_status", "status": "idle"})
            raise HTTPException(503, str(exc)) from exc
        except Exception:
            await hub.broadcast(room, {"type": "ai_status", "status": "idle"})
            raise
        try:
            new_state, summary = await commit_ops(pid, branch, result.ops, f"AI ({actor})")
        except (OpError, ConflictError) as exc:
            await hub.broadcast(room, {"type": "ai_status", "status": "idle"})
            return {"applied": False, "error": str(exc), "explanation": result.explanation,
                    "questions": result.questions, "engine": result.engine, "ops": result.ops}
        await hub.broadcast(room, {"type": "ai_status", "status": "idle"})
        return {"applied": True, "summary": summary, "explanation": result.explanation,
                "questions": result.questions, "engine": result.engine, "state": new_state.model_dump()}

    @app.post("/api/projects/{pid}/ai/command")
    async def ai_command(pid: str, body: CommandIn, branch: str = "main"):
        return await run_ai_command(pid, branch, body.text, body.selection, body.actor)

    @app.post("/api/projects/{pid}/ai/ask")
    async def ai_ask(pid: str, body: AskIn, branch: str = "main"):
        project = project_or_404(pid)
        state = state_or_404(pid, branch)
        history = store.list_versions(pid)[:30]
        acts = store.list_activity(pid, branch, 60)
        context = (f"<project name=\"{project['name']}\" branch=\"{branch}\">\n"
                   f"<model>\n{ai._state_for_prompt(state)}\n</model>\n"
                   f"<analysis>\n{json.dumps(full_analysis(state)['health'])}\n</analysis>\n"
                   f"<versions>\n{json.dumps(history, default=str)}\n</versions>\n"
                   f"<activity>\n{json.dumps(acts, default=str)}\n</activity>\n</project>")
        try:
            answer = await asyncio.to_thread(ai.claude_ask, context, body.question)
        except ai.AIUnavailable as exc:
            raise HTTPException(503, f"{exc}. Set GEMINI_API_KEY (or ANTHROPIC_API_KEY / OPENAI_API_KEY) to use the project assistant.") from exc
        return {"answer": answer}

    # ---- cloud conversion ----
    @app.post("/api/projects/{pid}/convert")
    async def convert(pid: str, body: ConvertIn, branch: str = "main"):
        ops, skipped = catalog.convert_ops(state_or_404(pid, branch), body.target)
        _, summary = await commit_ops(pid, branch, ops, f"{body.actor} (convert to {catalog.VENDORS[body.target]})")
        return {"changed": len(ops), "summary": summary, "skipped": skipped}

    # ---- guided design chat ----
    @app.post("/api/projects/{pid}/chat")
    async def chat_turn(pid: str, body: ChatIn, branch: str = "main"):
        state = state_or_404(pid, branch)
        return await asyncio.to_thread(chat.next_turn, body.turns[-60:], state)

    @app.post("/api/projects/{pid}/chat/apply")
    async def chat_apply(pid: str, body: ChatApplyIn, branch: str = "main"):
        project_or_404(pid)
        target = branch
        if body.mode == "branch":
            target = body.branch_name or f"draft/{chat.slugify(body.option.get('name', 'option'))}"
            if store.get_state(pid, target) is not None:
                raise HTTPException(409, f"branch '{target}' already exists")
            store.create_branch(pid, target, state_or_404(pid, branch), store.head_version(pid, branch))
        ops = chat.architecture_ops(state_or_404(pid, target), body.option, body.requirements,
                                    replace=body.mode != "merge")
        new_state, summary = await commit_ops(pid, target, ops, f"{body.actor} (design chat)")
        version = store.commit(pid, target, f"Applied architecture option: {body.option.get('name', 'option')}",
                               body.actor, new_state)
        await hub.broadcast(Hub.key(pid, target), {"type": "versions", "actor": body.actor})
        return {"branch": target, "version": version["number"], "summary": summary, "state": new_state.model_dump()}

    # ---- internal: collab server ↔ API (shared-secret authenticated) ----
    def internal_auth(token: str | None) -> None:
        if not collab_sync.INTERNAL_TOKEN:
            raise HTTPException(503, "ARCHFLOW_INTERNAL_TOKEN is not configured")
        if not token or not secrets.compare_digest(token, collab_sync.INTERNAL_TOKEN):
            raise HTTPException(401, "invalid internal token")

    @app.get("/api/internal/ydocs/{name:path}")
    def get_ydoc(name: str, x_internal_token: str | None = Header(None)):
        internal_auth(x_internal_token)
        pid, branch = collab_sync.parse_doc_name(name)
        data = store.get_ydoc(pid, branch)
        if data is None:
            raise HTTPException(404, "no stored document")
        return Response(content=data, media_type="application/octet-stream")

    @app.put("/api/internal/ydocs/{name:path}", status_code=204)
    async def put_ydoc(name: str, request: Request, x_internal_token: str | None = Header(None)):
        internal_auth(x_internal_token)
        pid, branch = collab_sync.parse_doc_name(name)
        state_or_404(pid, branch)
        store.put_ydoc(pid, branch, await request.body())

    @app.put("/api/internal/projection/{name:path}")
    async def put_projection(name: str, body: ProjectionIn, x_internal_token: str | None = Header(None)):
        """The live document's current content; becomes the branch state used by AI, MCP and analysis."""
        internal_auth(x_internal_token)
        pid, branch = collab_sync.parse_doc_name(name)
        incoming, dropped = collab_sync.sanitize(body.state)
        with store.transaction(pid, branch) as current:
            if current is None:
                raise HTTPException(404, "branch not found")
            merged, summary = collab_sync.merge_projection(current, incoming)
            if summary:
                store.save_state(pid, branch, merged)
                actor = ", ".join(sorted(set(body.actors))) or "collaborator"
                store.log_activity(pid, branch, actor, summary[:30], merged.rev)
        if summary:
            await hub.broadcast(Hub.key(pid, branch), {"type": "activity", "actor": body.actors})
        return {"changed": bool(summary), "rev": merged.rev, "dropped": dropped}

    # ---- realtime ----
    @app.websocket("/ws/{pid}/{branch}")
    async def ws_endpoint(ws: WebSocket, pid: str, branch: str, name: str = "anonymous", color: str = "#888"):
        await ws.accept()
        if not store.get_project(pid) or store.get_state(pid, branch) is None:
            await ws.close(code=4404)
            return
        room = Hub.key(pid, branch)
        await ws.send_text(json.dumps({"type": "hello", "client_id": id(ws)}))
        await hub.join(room, ws, {"name": name[:40], "color": color[:20]})
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                if msg.get("type") in ("cursor", "selection"):
                    # Ephemeral awareness data: relay only, never persisted.
                    await hub.broadcast(room, {**msg, "client_id": id(ws), "name": name, "color": color}, exclude=ws)
        except (WebSocketDisconnect, json.JSONDecodeError):
            pass
        finally:
            await hub.leave(room, ws)

    # ---- remote MCP server (Streamable HTTP + OAuth 2.1) ----
    mcp = build_remote_mcp(Services(store=store, commit_ops=commit_ops, commit_version=commit_version,
                                    ai_command=run_ai_command))
    provider: auth.ArchFlowOAuthProvider = mcp._archflow_provider
    app.state.mcp = mcp

    @app.get("/oauth/consent", response_class=HTMLResponse)
    def consent_form(request: str):
        req = provider.pending_request(request)
        if not req:
            return HTMLResponse("<p>This authorization request has expired. Start the connection again from your app.</p>", 400)
        return auth.consent_page(req, request, store.list_orgs())

    @app.post("/oauth/consent")
    async def consent_submit(req: Request):
        form = await req.form()
        request_id = str(form.get("request", ""))
        org_id = str(form.get("org_id", ""))
        user_name = str(form.get("user_name", "")).strip()[:80]
        approve = form.get("decision") == "approve"
        if approve and (not user_name or not store.get_org(org_id)):
            pending = provider.pending_request(request_id)
            if not pending:
                return HTMLResponse("<p>This authorization request has expired.</p>", 400)
            return HTMLResponse(auth.consent_page(pending, request_id, store.list_orgs(),
                                                  "Enter your name and choose an organization."), 400)
        scopes = [auth.SCOPE_READ, auth.SCOPE_WRITE] if form.get("access") == "write" else [auth.SCOPE_READ]
        try:
            url = provider.complete_consent(request_id, approve, org_id, user_name or "anonymous", scopes)
        except ValueError as exc:
            return HTMLResponse(f"<p>{exc}</p>", 400)
        if approve:
            store.add_member(org_id, user_name, "member")
        return RedirectResponse(url, status_code=302)

    # Personal access tokens and connected apps (TODO(auth): restrict to org admins once users sign in)
    @app.get("/api/orgs/{org_id}/tokens")
    def list_tokens(org_id: str):
        if not store.get_org(org_id):
            raise HTTPException(404, "org not found")
        clients: dict[str, str] = {}

        def client_name(cid: str | None) -> str:
            if not cid:
                return ""
            if cid not in clients:
                c = store.get_oauth_client(cid) or {}
                clients[cid] = c.get("client_name") or cid
            return clients[cid]

        out = []
        for t in store.list_tokens(org_id):
            if t["kind"] == "access":
                continue  # short-lived; represented by the app's refresh token
            out.append({**t, "client_name": client_name(t["client_id"]),
                        "access": "write" if auth.SCOPE_WRITE in t["scopes"] else "read"})
        return out

    @app.post("/api/orgs/{org_id}/tokens", status_code=201)
    def create_token(org_id: str, body: TokenIn):
        if not store.get_org(org_id):
            raise HTTPException(404, "org not found")
        scopes = [auth.SCOPE_READ, auth.SCOPE_WRITE] if body.access == "write" else [auth.SCOPE_READ]
        secret, record = auth.create_pat(store, org_id, body.user_name, body.name, scopes, body.expires_in_days)
        return {"token": secret, **record}

    @app.delete("/api/orgs/{org_id}/tokens/{token_id}", status_code=204)
    def revoke_token(org_id: str, token_id: str):
        t = store.get_token_by_id(token_id)
        if not t or t["org_id"] != org_id or not store.revoke_token_id(token_id):
            raise HTTPException(404, "token not found")
        if t["kind"] == "refresh":  # disconnecting an app also ends its current sessions
            for a in store.list_tokens(org_id, "access"):
                if a["client_id"] == t["client_id"] and a["user_name"] == t["user_name"]:
                    store.revoke_token_id(a["id"])

    @app.get("/api/mcp-info")
    def mcp_info():
        return {"url": auth.MCP_URL, "issuer": auth.PUBLIC_URL, "scopes": auth.ALL_SCOPES}

    # Mounted last so every API route above takes precedence. Serves /mcp, the OAuth endpoints
    # (/authorize, /token, /register, /revoke) and the discovery documents under /.well-known/.
    app.mount("/", mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                            transport_security=mcp._archflow_security))
    return app


app = create_app()
