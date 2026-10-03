"""Remote MCP server (Streamable HTTP at /mcp, OAuth 2.1 protected).

Connect from:
  * Claude Code:     claude mcp add --transport http archflow https://<host>/mcp
  * Claude.ai / Desktop: Settings → Connectors → Add custom connector → https://<host>/mcp
  * Any MCP client with a personal access token: header ``Authorization: Bearer afp_…``

Tools run in-process through the same operation pipeline as the canvas, so every agent edit is validated,
logged under the agent's identity, and appears live on everyone's canvas. Each token is bound to one
organization; tools refuse projects from other orgs, and editing tools require the ``archflow:write`` scope.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from . import auth, catalog, lld
from . import decisions as decisions_mod
from .analysis import full_analysis, trace_requirement
from .diff import diff_states, diff_to_text
from .models import NODE_TYPES, ProjectState
from .ops import OpError, ConflictError, resolve_node
from .templates import TEMPLATES, build_template
from .vibe import TARGETS, build_prompt

log = logging.getLogger("archflow.mcp")


@dataclass
class Services:
    """What the MCP tools need from the API (provided by main.create_app)."""
    store: Any
    commit_ops: Callable[[str, str, list[dict[str, Any]], str], Awaitable[tuple[ProjectState, list[str]]]]
    commit_version: Callable[[str, str, str, str], Awaitable[dict[str, Any]]]
    ai_command: Callable[[str, str, str, list[str], str], Awaitable[dict[str, Any]]]


INSTRUCTIONS = """ArchFlow holds a team's software architecture as structured data: components (nodes), connections \
(edges) and requirements, versioned per branch. Use get_architecture before editing. Edits appear live on the \
team's canvas, attributed to you. For coding work: list_tasks → get_task_brief → implement → report_progress."""


class Principal:
    def __init__(self):
        tok = get_access_token()
        if tok is None:
            raise ToolError("Not authenticated")
        claims = tok.claims or {}
        self.org_id: str = claims.get("org_id", "")
        self.user: str = tok.subject or "agent"
        self.client: str = tok.client_id
        self.scopes = set(tok.scopes)

    @property
    def actor(self) -> str:
        return f"{self.user} via MCP"

    def require_write(self):
        if auth.SCOPE_WRITE not in self.scopes:
            raise ToolError("This connection is read-only. Reconnect with edit access to change the architecture.")


def build_remote_mcp(services: Services) -> MCPServer:
    store = services.store
    provider = auth.ArchFlowOAuthProvider(store)
    allowed_hosts = [h.strip() for h in os.environ.get("ARCHFLOW_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    mcp = MCPServer(
        "archflow",
        instructions=INSTRUCTIONS,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=auth.PUBLIC_URL,
            resource_server_url=auth.MCP_URL,
            validate_token_resource=True,
            required_scopes=[auth.SCOPE_READ],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=auth.ALL_SCOPES, default_scopes=auth.ALL_SCOPES),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )
    mcp._archflow_provider = provider  # used by the consent routes in main.py
    mcp._archflow_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *allowed_hosts],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                         *[f"https://{h}" for h in allowed_hosts]],
    )

    # ---- helpers ----
    def project_for(p: Principal, project_id: str, branch: str = "main") -> tuple[dict, ProjectState]:
        project = store.get_project(project_id)
        if not project or project.get("org_id") != p.org_id:
            raise ToolError(f"Project '{project_id}' not found in your organization")
        state = store.get_state(project_id, branch)
        if state is None:
            raise ToolError(f"Branch '{branch}' not found")
        return project, state

    def compact(state: ProjectState) -> dict[str, Any]:
        return {
            "rev": state.rev,
            "components": [{"id": n.id, "name": n.name, "type": n.type, "technology": n.technology,
                            "description": n.description, "requirement_ids": n.requirement_ids,
                            "parent_id": n.parent_id, **({"props": n.props} if n.props else {})}
                           for n in state.nodes.values()],
            "connections": [{"id": e.id, "source": e.source, "target": e.target, "protocol": e.protocol, "label": e.label}
                            for e in state.edges.values()],
            "requirements": [r.model_dump(exclude={"rev", "field_revs"}) for r in state.requirements.values()],
        }

    async def edit(project_id: str, branch: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
        p = Principal()
        p.require_write()
        project_for(p, project_id, branch)
        try:
            state, summary = await services.commit_ops(project_id, branch, ops, p.actor)
        except (OpError, ConflictError) as exc:
            raise ToolError(str(exc)) from exc
        return {"ok": True, "changes": summary, "rev": state.rev}

    # ---- read tools ----
    @mcp.tool()
    def whoami() -> dict[str, Any]:
        """Who this connection acts as: user, organization and access level."""
        p = Principal()
        org = store.get_org(p.org_id) or {}
        return {"user": p.user, "organization": {"id": p.org_id, "name": org.get("name")},
                "access": "read-write" if auth.SCOPE_WRITE in p.scopes else "read-only"}

    @mcp.tool()
    def list_projects() -> list[dict[str, Any]]:
        """Projects in your organization."""
        p = Principal()
        return [{k: x[k] for k in ("id", "name", "description", "created_at")} for x in store.list_projects(p.org_id)]

    @mcp.tool()
    def get_architecture(project_id: str, branch: str = "main") -> dict[str, Any]:
        """The full structured model of a project branch: components, connections and requirements."""
        p = Principal()
        project, state = project_for(p, project_id, branch)
        return {"project": {"id": project["id"], "name": project["name"]}, "branch": branch,
                "branches": [b["name"] for b in store.list_branches(project_id)], **compact(state)}

    @mcp.tool()
    def get_requirements(project_id: str, branch: str = "main") -> list[dict[str, Any]]:
        """Structured requirements with actors, acceptance criteria, priority and status."""
        _, state = project_for(Principal(), project_id, branch)
        return compact(state)["requirements"]

    @mcp.tool()
    def validate_architecture(project_id: str, branch: str = "main") -> dict[str, Any]:
        """Requirement lint, architecture checks, traceability and a health summary."""
        _, state = project_for(Principal(), project_id, branch)
        return full_analysis(state)

    @mcp.tool()
    def list_versions(project_id: str) -> list[dict[str, Any]]:
        """Committed versions of the project (all branches), newest first."""
        project_for(Principal(), project_id)
        return store.list_versions(project_id)

    @mcp.tool()
    def get_architecture_diff(project_id: str, from_version: int, to_version: int | None = None,
                              branch: str = "main") -> str:
        """What changed between two versions (or between a version and the branch's current state)."""
        _, current = project_for(Principal(), project_id, branch)
        old = store.get_version(project_id, from_version)
        if not old:
            raise ToolError(f"version {from_version} not found")
        new = store.get_version(project_id, to_version)["state"] if to_version else current
        return diff_to_text(diff_states(old["state"], new))

    @mcp.tool()
    def list_tasks(project_id: str, branch: str = "main", include_done: bool = False) -> list[dict[str, Any]]:
        """Implementation tasks: requirements with the components that implement them, highest priority first."""
        _, state = project_for(Principal(), project_id, branch)
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        tasks = []
        for r in state.requirements.values():
            if r.status == "implemented" and not include_done:
                continue
            nodes = trace_requirement(state, r.id)["node_ids"]
            tasks.append({"id": r.id, "title": r.title, "priority": r.priority, "status": r.status, "kind": r.kind,
                          "components": [state.nodes[n].name for n in nodes],
                          "acceptance_criteria": len(r.acceptance_criteria)})
        return sorted(tasks, key=lambda t: (order.get(t["priority"], 9), t["id"]))

    @mcp.tool()
    def get_task_brief(project_id: str, requirement_id: str, target: str = "claude-code", branch: str = "main") -> str:
        """Implementation brief for one requirement: affected components, interfaces, acceptance criteria and tests.
        target: claude-code, codex, gemini-cli, aider or generic."""
        project, state = project_for(Principal(), project_id, branch)
        if requirement_id not in state.requirements:
            raise ToolError(f"requirement {requirement_id} not found")
        return build_prompt(state, project["name"], [], [requirement_id], target if target in TARGETS else "generic")

    @mcp.tool()
    def generate_coding_prompt(project_id: str, component_ids: list[str] | None = None, target: str = "claude-code",
                               branch: str = "main") -> str:
        """Implementation brief for selected components (or the whole system when none are given)."""
        project, state = project_for(Principal(), project_id, branch)
        return build_prompt(state, project["name"], component_ids or [], [], target if target in TARGETS else "generic")

    @mcp.tool()
    def search_catalog(query: str = "", vendor: str = "", limit: int = 25) -> list[dict[str, Any]]:
        """Find concrete services to use as components (AWS, Azure, GCP, open source, SaaS).
        vendor: aws, azure, gcp, oss, saas or generic. Pass the result's id as catalog_id to add_component."""
        Principal()
        q = query.lower()
        hits = [c for c in catalog.CATALOG
                if (not vendor or c["vendor"] == vendor)
                and (not q or q in f"{c['name']} {c['kind']} {c['kind_label']} {c['category']}".lower())]
        return hits[:max(1, min(limit, 100))]

    # ---- write tools ----
    @mcp.tool()
    async def create_project(name: str, description: str = "", template: str = "blank") -> dict[str, Any]:
        """Create a project in your organization. template: blank or ai-customer-support."""
        p = Principal()
        p.require_write()
        project = store.create_project(name, description, build_template(template if template in TEMPLATES else "blank"),
                                       org_id=p.org_id)
        return {"id": project["id"], "name": project["name"]}

    @mcp.tool()
    async def add_component(project_id: str, name: str, type: str = "service", technology: str = "",
                            description: str = "", requirement_ids: list[str] | None = None,
                            x: float | None = None, y: float | None = None, catalog_id: str = "",
                            branch: str = "main", parent: str = "") -> dict[str, Any]:
        """Add a component. type: client, gateway, service, database, cache, queue, storage, agent, llm, mcp,
        external, security or note; inside a component's low-level design also component, endpoint, table or
        event. Position is chosen automatically if x/y are omitted. catalog_id (from search_catalog) fills in
        type, technology and vendor for a concrete service such as aws-lambda. parent (id or name) places it in
        that component's internal diagram; connections only join elements of the same diagram."""
        node: dict[str, Any] = {"name": name, "type": type if type in NODE_TYPES else "service", "technology": technology,
                                "description": description, "requirement_ids": requirement_ids or [],
                                "parent_id": parent}
        entry = catalog.BY_ID.get(catalog_id)
        if entry:
            node.update(type=entry["type"], vendor=entry["vendor"], catalog_id=entry["id"],
                        technology=technology or entry["name"])
        if x is not None and y is not None:
            node["position"] = {"x": x, "y": y}
        return await edit(project_id, branch, [{"op": "add_node", "node": node}])

    @mcp.tool()
    async def update_component(project_id: str, component: str, name: str | None = None, type: str | None = None,
                               technology: str | None = None, description: str | None = None,
                               requirement_ids: list[str] | None = None, branch: str = "main") -> dict[str, Any]:
        """Change a component (by id or name). Only the given fields change."""
        changes = {k: v for k, v in {"name": name, "type": type, "technology": technology, "description": description,
                                     "requirement_ids": requirement_ids}.items() if v is not None}
        return await edit(project_id, branch, [{"op": "update_node", "id": component, "changes": changes}])

    @mcp.tool()
    async def remove_component(project_id: str, component: str, branch: str = "main") -> dict[str, Any]:
        """Remove a component (by id or name) and its connections."""
        return await edit(project_id, branch, [{"op": "remove_node", "id": component}])

    @mcp.tool()
    async def connect_components(project_id: str, source: str, target: str, protocol: str = "", label: str = "",
                                 branch: str = "main") -> dict[str, Any]:
        """Connect source → target (ids or names), e.g. protocol REST, gRPC, Kafka, MCP, A2A."""
        return await edit(project_id, branch, [{"op": "add_edge", "edge": {
            "source": source, "target": target, "protocol": protocol, "label": label}}])

    @mcp.tool()
    async def disconnect_components(project_id: str, source: str, target: str, branch: str = "main") -> dict[str, Any]:
        """Remove the connection source → target."""
        return await edit(project_id, branch, [{"op": "remove_edge", "source": source, "target": target}])

    @mcp.tool()
    async def create_requirement(project_id: str, title: str, description: str = "", kind: str = "functional",
                                 category: str = "", priority: str = "medium", actors: list[str] | None = None,
                                 acceptance_criteria: list[str] | None = None, branch: str = "main") -> dict[str, Any]:
        """Add a requirement. kind: functional | non_functional. priority: low | medium | high | critical."""
        return await edit(project_id, branch, [{"op": "add_requirement", "requirement": {
            "title": title, "description": description, "kind": kind, "category": category, "priority": priority,
            "actors": actors or [], "acceptance_criteria": acceptance_criteria or []}}])

    @mcp.tool()
    async def update_requirement(project_id: str, requirement_id: str, title: str | None = None,
                                 description: str | None = None, priority: str | None = None, status: str | None = None,
                                 acceptance_criteria: list[str] | None = None, branch: str = "main") -> dict[str, Any]:
        """Change a requirement. status: draft | review | approved | implemented."""
        changes = {k: v for k, v in {"title": title, "description": description, "priority": priority, "status": status,
                                     "acceptance_criteria": acceptance_criteria}.items() if v is not None}
        return await edit(project_id, branch, [{"op": "update_requirement", "id": requirement_id, "changes": changes}])

    @mcp.tool()
    async def report_progress(project_id: str, requirement_id: str, status: str, note: str = "", pr_url: str = "",
                              branch: str = "main") -> dict[str, Any]:
        """Report work on a task. status: in_progress | review | implemented. Shows in the project's activity."""
        p = Principal()
        p.require_write()
        _, state = project_for(p, project_id, branch)
        if requirement_id not in state.requirements:
            raise ToolError(f"requirement {requirement_id} not found")
        if status not in ("in_progress", "review", "implemented"):
            raise ToolError("status must be in_progress, review or implemented")
        result: dict[str, Any] = {"ok": True}
        if status in ("review", "implemented"):
            result = await edit(project_id, branch, [{"op": "update_requirement", "id": requirement_id,
                                                      "changes": {"status": status}}])
        line = f"{requirement_id}: {status.replace('_', ' ')}" + (f" — {note}" if note else "") + (f" ({pr_url})" if pr_url else "")
        store.log_activity(project_id, branch, p.actor, [line], state.rev)
        return {**result, "logged": line}

    @mcp.tool()
    async def convert_cloud(project_id: str, target: str, branch: str = "main") -> dict[str, Any]:
        """Swap every recognised component for its equivalent on another platform.
        target: aws, azure, gcp or oss (self-hosted open source). Third-party SaaS stays as it is."""
        p = Principal()
        p.require_write()
        _, state = project_for(p, project_id, branch)
        try:
            ops, skipped = catalog.convert_ops(state, target)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        out = await edit(project_id, branch, ops) if ops else {"ok": True, "changes": []}
        return {**out, "skipped": skipped}

    @mcp.tool()
    async def generate_lld(project_id: str, component: str, branch: str = "main", replace: bool = False) -> dict[str, Any]:
        """Generate the low-level design (C4 component level) inside a component (by id or name): internal
        components, API endpoints, DB tables and domain events, connected. The elements get parent_id = the
        component's id. Set replace=true to regenerate an existing design."""
        p = Principal()
        p.require_write()
        _, state = project_for(p, project_id, branch)
        try:
            ops, info = await asyncio.to_thread(lld.build, state, component, replace, p.org_id)
        except KeyError as exc:
            raise ToolError(f"component '{component}' not found") from exc
        except lld.LLDError as exc:
            raise ToolError(str(exc)) from exc
        out = await edit(project_id, branch, ops)
        return {**out, **info}

    @mcp.tool()
    def get_component_views(project_id: str, component: str, branch: str = "main") -> dict[str, Any]:
        """Views derived from a component's design: its API endpoints (plus OpenAPI text), data model (tables,
        relations, Mermaid erDiagram) and the main request flow as a Mermaid sequenceDiagram."""
        _, state = project_for(Principal(), project_id, branch)
        node = resolve_node(state, component)
        if not node:
            raise ToolError(f"component '{component}' not found")
        return lld.views(state, node)

    @mcp.tool()
    async def commit_version(project_id: str, message: str, branch: str = "main") -> dict[str, Any]:
        """Snapshot the branch as a new version."""
        p = Principal()
        p.require_write()
        project_for(p, project_id, branch)
        v = await services.commit_version(project_id, branch, message, p.actor)
        return {"version": v["number"], "branch": branch, "message": message}

    @mcp.tool()
    async def architecture_command(project_id: str, instruction: str, branch: str = "main") -> dict[str, Any]:
        """Apply a natural-language change, e.g. 'Add Redis between Order Service and Orders DB'."""
        p = Principal()
        p.require_write()
        project_for(p, project_id, branch)
        out = await services.ai_command(project_id, branch, instruction, [], p.actor)
        out.pop("state", None)
        return out

    # ---- architecture decisions (ADRs) ----
    @mcp.tool()
    def list_decisions(project_id: str) -> list[dict[str, Any]]:
        """Architecture decision records (ADR-001, ...) of a project with status and chosen alternative."""
        project_for(Principal(), project_id)
        return [{k: d[k] for k in ("id", "title", "status", "chosen", "author", "created_at", "updated_at")}
                for d in store.list_decisions(project_id)]

    @mcp.tool()
    def get_decision(project_id: str, decision_id: str) -> dict[str, Any]:
        """One decision record: context, decision, consequences, alternatives, linked requirements and evaluation."""
        project_for(Principal(), project_id)
        d = store.get_decision(project_id, decision_id)
        if not d:
            raise ToolError(f"decision {decision_id} not found")
        return d

    @mcp.tool()
    async def create_decision(project_id: str, title: str, context: str = "", decision: str = "",
                              consequences: str = "", status: str = "proposed",
                              alternatives: list[dict[str, Any]] | None = None, chosen: str = "",
                              requirement_ids: list[str] | None = None,
                              component_ids: list[str] | None = None) -> dict[str, Any]:
        """Record an architecture decision (ADR). status: proposed | accepted | rejected | superseded.
        alternatives: [{name, branch?, summary, pros: [..], cons: [..]}]; chosen: an alternative name or branch."""
        p = Principal()
        p.require_write()
        project_for(p, project_id)
        try:
            alts = [decisions_mod.Alternative.model_validate(a).model_dump() for a in alternatives or []]
            return decisions_mod.create_decision(store, project_id, {
                "title": title, "status": status, "context": context, "decision": decision,
                "consequences": consequences, "alternatives": alts, "chosen": chosen,
                "requirement_ids": requirement_ids or [], "node_ids": component_ids or []}, p.actor)
        except ValueError as exc:  # includes pydantic.ValidationError
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    async def evaluate_alternatives(project_id: str, branches: list[str], create_decision: bool = True,
                                    title: str = "") -> dict[str, Any]:
        """Compare 2–4 draft branches against the project's requirements (Jev decision API, else an LLM, else
        an offline heuristic; 'engine' says which). Scores cost, scalability, performance, security, reliability,
        operability and delivery speed 1–5 and recommends one. With create_decision, records a proposed ADR."""
        p = Principal()
        if create_decision:
            p.require_write()
        project, _ = project_for(p, project_id)
        try:
            result = await asyncio.to_thread(decisions_mod.evaluate_branches, store, project_id, branches,
                                             "main", project.get("org_id"))
        except KeyError as exc:
            raise ToolError(f"branch '{exc.args[0]}' not found") from exc
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        result.pop("raw", None)
        if create_decision:
            d = decisions_mod.decision_from_evaluation(store, project_id, result, p.actor, title or None)
            result["decision"] = {"id": d["id"], "title": d["title"], "status": d["status"], "chosen": d["chosen"]}
        return result

    return mcp
