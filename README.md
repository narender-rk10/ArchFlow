# ArchFlow

**Open-source AI requirements & system-architecture platform.**

> Business idea → Requirements → Architecture → Canvas → Vibe Coding → Code → Tests → Deployment

ArchFlow treats requirements, architecture, decisions and versions as **one structured, versioned,
collaborative project model**, not separate documents or pictures. Humans dragging boxes, the built-in AI
and external agents over MCP all edit the same model through the same operation pipeline, so every change
is validated, versioned, conflict-checked and shown live to everyone.

## What's in this MVP

| Area | What works |
|---|---|
| Project workspace | Projects, templates (blank, AI customer-support), per-branch working state |
| Guided design chat | Describe an idea; ArchFlow asks multiple-choice questions (single/multi-select, recommended option, “Other”), then offers 2–4 ranked architectures with pros/cons, cost and complexity. Pick one to apply it to the canvas or open it as a draft branch. Works offline with a built-in question bank; adaptive with an LLM |
| Organizations | Orgs with members and org-scoped projects (no authentication yet; users are display names) |
| Requirement model | Structured FR/NFR objects: actors, acceptance criteria, priority, status, category |
| Requirement completeness | Lint for vague terms ("fast", "scalable"…), missing acceptance criteria/actors, missing NFR categories |
| Architecture canvas | React Flow canvas: drag from palette, connect, move, multi-select, delete, copy/paste/duplicate, search, zoom/pan, minimap |
| Component catalog | 150 components: generic building blocks plus AWS, Azure, Google Cloud, self-hosted open source and SaaS services, each mapped to a cloud-neutral kind. Searchable palette with vendor filters, vendor badges on nodes, "swap for an equivalent" in the inspector, and **Convert to AWS / Azure / Google Cloud / open source** for the whole canvas (also `convert_cloud` and `search_catalog` over MCP). Official vendor icon sets are next |
| Canvas as structured data | Nodes/edges are typed objects (`service`, `database`, `agent`, `llm`, `mcp`, …) with technology, description, linked requirements |
| HLD → LLD drill-down (C4) | Double-click a component (or **Open internals ↘**) to open its own low-level diagram; a breadcrumb leads back. **Generate low-level design** creates controller/service/repository components, API endpoints (inferred from the component and its requirements), tables for connected databases and events for connected queues — with the LLM when configured, a template otherwise. Inspector **Views**: API list (OpenAPI text), data model (Mermaid `erDiagram`) and main request flow (Mermaid `sequenceDiagram`). Connections stay within one diagram level; deleting a component deletes its internals. Also `generate_lld` / `get_component_views` over MCP |
| AI edits the canvas | Natural-language commands ("Add Redis between Order Service and Orders DB") become operations on the model. Uses Claude when credentials exist; a built-in rule parser covers common commands offline |
| AI requirements interviewer | Describe a product idea; Claude drafts requirements + architecture and asks clarifying questions |
| AI project assistant | "Why was Redis added?", "What happens if the DB fails?", one-click architecture review. Answers come from the model, version history and activity log |
| Real-time collaboration | Google Docs-style editing on a shared Yjs document (Hocuspocus server): edits merge automatically, offline edits sync on reconnect, undo/redo (Ctrl+Z / Ctrl+Y), presence avatars, live cursors and selection outlines. AI, MCP and API edits are merged into the live document field by field, so they never overwrite what people are typing |
| Concurrent edits | Different fields of the same component merge; if someone changes the field you are editing, the field shows their value with a "Use theirs" option. API/MCP callers can still send `base_rev` for strict conflict detection (HTTP 409) |
| Dark / light mode | Toggle in the top bar (system → light → dark), remembered per browser |
| Versioning | Commit snapshots, version list, restore, create branch from any version, switch branches |
| Architecture & requirement diff | Structural diff between any versions/branches, rendered **on the canvas** (added / changed / removed ghosts) plus text |
| Traceability | Requirement ↔ component links; click a requirement to highlight everything implementing it; untraced requirements flagged |
| Architecture validation | Orphans, client→DB access, shared databases, unguarded LLMs, missing auth or PII controls, missing gateway |
| Health dashboard | Requirements completeness/approval, traceability %, architecture score, issue counts |
| Vibe coding | Implementation brief for Claude Code / Codex / Gemini CLI / Aider from the whole system, selected components, or a requirement (pulls in traced context, interfaces, stubs, acceptance criteria → tests) |
| Remote MCP server | Streamable HTTP at `/mcp` with OAuth 2.1 (works with Claude Code, Claude.ai, Claude Desktop and other MCP clients) or personal access tokens; 29 tools, org-scoped, read-only or read & edit. Agent edits show up live on everyone's canvas |
| Activity history | Who changed what, and when, per branch |
| Architecture decisions | **Decisions** tab: ADRs (ADR-001, …) with status, context, decision, consequences, alternatives and linked requirements. **Compare drafts** scores 2–4 branches against the requirements/NFRs on cost, scalability, performance, security, reliability, operability and delivery speed (1–5 matrix + recommendation) using the [Jev](https://thejevai.com/docs) decision API (`JEV_API_KEY`), else the LLM, else an offline heuristic; the engine is always shown. Record the result as an ADR and **adopt** the chosen draft into `main` (safety version first, then a new version). REST under `/api/projects/{id}/decisions` and `/evaluate`; MCP `evaluate_alternatives` & co. |

## Quick start

Prerequisites: Python 3.11+ with [uv](https://docs.astral.sh/uv/), Node 20+.

```bash
# 0. Local settings: copy the template and fill in the blanks (the file is git-ignored)
cp .env.example .env
#    set ARCHFLOW_INTERNAL_TOKEN and POSTGRES_PASSWORD to random values, e.g.
#    python -c "import secrets; print(secrets.token_urlsafe(32))"
#    and put LLM keys (GEMINI_API_KEY, …) in .env or backend/.env

# 1. Backend (http://localhost:8000)
cd backend
uv sync
uv run uvicorn archflow.main:app --reload --port 8000

# 2. Collaboration server (ws://localhost:1234), in a second terminal
cd collab
npm install
npm run dev

# 3. Frontend (http://localhost:5173), in a third terminal
cd frontend
npm install
npm run dev
```

Open http://localhost:5173, enter your name, and create a project from the **AI customer-support** template.
Open a second browser window with a different name to see multiplayer.

### Enabling the LLM

All AI calls go through `backend/archflow/llm.py`, a LiteLLM gateway: **Gemini first**, then Claude, then
OpenAI. Only providers whose key is set are used. Rate limits (429) are handled with LiteLLM retries and cooldowns, our own
exponential backoff with jitter (honouring `Retry-After`), cross-provider fallback, a per-model circuit
breaker and a per-org request limit. Without any key, canvas commands and the design chat use built-in
offline logic, and the Ask assistant is disabled.

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` / `GEMINI_API_KEYS` | – | Gemini key, or comma-separated keys to load-balance |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | – | Fallback providers |
| `ARCHFLOW_AI` | `auto` | `auto` (LLM, fall back to offline), `llm`, `offline` |
| `ARCHFLOW_MODEL_REASONING` | `gemini/gemini-3.1-pro-preview` | Main model (set `gemini/gemini-2.5-pro` for a stable model) |
| `ARCHFLOW_MODEL_FAST` | `gemini/gemini-3.8-flash` | Fast model |
| `ARCHFLOW_ORG_RPM` | `60` | LLM requests per minute per org |
| `LANGFUSE_PUBLIC_KEY` … | – | Enables LLM tracing |
| `JEV_API_KEY` | – | Jev decision API for comparing drafts (falls back to the LLM, then an offline heuristic) |
| `DATABASE_URL` | – | `postgresql://…` switches storage to Postgres |
| `ARCHFLOW_DB` | `backend/data/archflow.db` | SQLite path when `DATABASE_URL` is unset |

See `.env.example` for the full list.

### Running on Postgres

```bash
docker compose up -d postgres redis
DATABASE_URL=postgresql://archflow:<POSTGRES_PASSWORD>@localhost:5432/archflow uv run uvicorn archflow.main:app --port 8000
```

Migrations in `backend/archflow/migrations/` apply automatically on startup. Branch edits take a row lock
(`SELECT … FOR UPDATE`), so several API processes can run side by side.

### Connecting Claude and other agents over MCP

The API serves a **remote MCP server** at `http://localhost:8000/mcp` (Streamable HTTP), protected by
OAuth 2.1: dynamic client registration, PKCE, a consent page, rotating refresh tokens and revocation.
Every connection is bound to one organization, as read-only or read & edit. Open **⌘ Connect AI** in the
app for copy-paste instructions and to manage tokens and connected apps.

```bash
# Claude Code: add the server, then run /mcp and choose "Authenticate"
claude mcp add --transport http archflow http://localhost:8000/mcp

# Headless (CI): use a personal access token created in the Connect AI dialog
claude mcp add --transport http archflow http://localhost:8000/mcp --header "Authorization: Bearer afp_…"
```

**Claude.ai / Claude Desktop** (Settings → Connectors → Add custom connector) connect from the internet, so
expose the API over HTTPS (for example a Cloudflare Tunnel or ngrok to port 8000) and set
`ARCHFLOW_PUBLIC_URL=https://your-host` and `ARCHFLOW_MCP_ALLOWED_HOSTS=your-host` before connecting to
`https://your-host/mcp`.

Tools (29): `search_catalog`, `convert_cloud`, `whoami`, `list_projects`, `get_architecture`, `get_requirements`, `validate_architecture`,
`list_versions`, `get_architecture_diff`, `list_tasks`, `get_task_brief`, `generate_coding_prompt`,
`create_project`, `add_component`, `update_component`, `remove_component`, `connect_components`,
`disconnect_components`, `create_requirement`, `update_requirement`, `report_progress`, `commit_version`,
`architecture_command`, low-level design: `generate_lld`, `get_component_views`, plus architecture decisions: `list_decisions`, `get_decision`, `create_decision`,
`evaluate_alternatives`. Agent edits go through the same pipeline as the canvas and appear live, attributed
to "*user* via MCP". For coding work an agent can loop **list_tasks → get_task_brief → implement →
report_progress** (status and PR link show up in the project activity).

A local stdio variant (`archflow-mcp`, configured in `.mcp.json`) is still available for offline use.

## Architecture

```text
  Browser (React Flow + Yjs)                       External agents (Claude Code, Codex…)
     │ live edits, presence, cursors (WebSocket)        │ MCP (stdio) → HTTP
     ▼                                                  ▼
  Collab server (Hocuspocus) ◄── field-level patches ── FastAPI ◄── AI (LiteLLM: Gemini → Claude → OpenAI)
     │  one Yjs doc per project branch                  │  ops.apply_ops: validated, atomic, logged
     └── debounced projection + binary doc ──────────►  │
                                                        ▼
                    Postgres / SQLite: orgs · projects · branch state · Yjs docs · versions · activity
```

Every mutation is a list of operations (`add_node`, `update_node`, `remove_node`, `add_edge`, `update_edge`,
`remove_edge`, `add_requirement`, `update_requirement`, `remove_requirement`). A batch is atomic. Update ops
carry `base_rev`, the revision the editor last saw, so the server can detect overlapping edits per field.

```text
backend/archflow/
  models.py      Node, Edge, Requirement, ProjectState
  ops.py         the single mutation pipeline + conflict detection
  diff.py        structural diff
  analysis.py    requirement lint, architecture validation, traceability, health
  ai.py          Claude tool-use → ops; offline command parser
  vibe.py        implementation-brief generator
  templates.py   starter architectures
  store.py       SQLite persistence
  main.py        REST + WebSocket API
  mcp_server.py  MCP server
frontend/src/
  components/Workspace.tsx   canvas, sync, orchestration
  components/*               nodes, palette, inspector, panels, command bar, dialogs
  realtime.ts                WebSocket hook
```

## Tests

```bash
cd backend && uv run pytest     # 46 tests; add TEST_DATABASE_URL=postgresql://… to also run the Postgres store tests
cd frontend && npm run build    # typecheck + production build
cd collab && npm test           # end-to-end: two live clients + MCP/AI edits (needs the API and collab server running)
```

## Roadmap

The full production plan (architecture, technology choices, phases) is in [docs/production-plan.md](docs/production-plan.md).

In rough order of leverage:

1. **ADRs** as first-class objects linked to components and requirements.
2. **Branch merge** with three-way diff and AI-assisted conflict resolution.
3. **Architecture alternatives + JEV evaluation**: generate options A/B/C as branches and score them.
4. **Comments, @mentions and approval workflows** on any object; RBAC.
5. **Repository analysis**: reconstruct architecture from code, drift detection, PR architecture review.
6. **Jira sync**: epics/stories generated from requirements, with status reflected on the canvas.
7. **Diagram generation** from the same model: sequence, C4, ER, deployment, agent graph, MCP topology.
8. **Document intake**: SRS/PDF → requirements; meeting transcripts → live canvas.
9. **Test generation, IaC generation, and documentation (SRS/BRD/runbooks)**.
10. **Postgres + CRDT (Yjs) layer** for larger teams and offline editing; component registry & marketplace.

## License

Apache-2.0
