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
| Accounts & organizations | Email + password accounts, HttpOnly session cookies, orgs with six roles (owner → viewer), invite links, org-scoped projects, per-org AI provider keys (encrypted) |
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
| Remote MCP server | Streamable HTTP at `/mcp` with OAuth 2.1 (works with Claude Code, Claude.ai, Claude Desktop and other MCP clients) or personal access tokens; 33 tools, org-scoped, read-only or read & edit. Agent edits show up live on everyone's canvas |
| Activity history | Who changed what, and when, per branch |
| Code ↔ architecture | **Code** button: analyse a public Git repository (or upload a graph made locally by `archflow-codegraph`) with [Graphify](https://pypi.org/project/graphifyy/) — tree-sitter code graph of files, symbols, calls and imports, Leiden communities, god nodes, import cycles — plus deterministic detections (languages, manifests, docker-compose, Dockerfiles, Kubernetes, Terraform, databases/caches/queues/LLM providers from imported libraries, entry points). **Reconstruct architecture** opens a `draft/from-code-<sha>` branch with services, clients and infrastructure, connections and catalog matches; components keep their code paths (linked to the repo at that commit in the inspector). **Drift** compares any branch with the code (in code but not designed, designed but not in code, undeclared connections, technology mismatches) and feeds the Issues tab; **change impact** maps changed files → dependent symbols → components → requirements. REST under `/api/projects/{id}/codegraph`, MCP `analyze_repository`, `get_code_summary`, `get_drift`, `get_change_impact` |
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

Open http://localhost:5173, create an account (the first account becomes owner of every existing org), and create a project from the **AI customer-support** template.
To see multiplayer, invite a second account (Members & invites → Create invite link) and open the link in a private window.

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

### Repository analysis (optional)

Analysing repositories needs the optional `codegraph` extra, which installs Graphify (`graphifyy==0.9.77`,
tree-sitter parsers for ~30 languages). Everything else works without it; the analyse endpoint then answers 503
with install instructions, while uploaded graphs, reconstruction, drift and impact keep working.

```bash
cd backend
uv sync --extra codegraph          # then restart the API
```

Analyse code that must not leave your machine with the CLI and upload only the graph (**Code → Private code? …**,
or `POST /api/projects/{id}/codegraph/upload`). The file contains paths, symbol names, relations and detected
infrastructure — no source code:

```bash
uv run archflow-codegraph ../path/to/repo -o archflow-codegraph.json
```

| Variable | Default | Purpose |
|---|---|---|
| `ARCHFLOW_CODEGRAPH_TIMEOUT` | `600` | Seconds the analysis worker process may run |
| `ARCHFLOW_ALLOW_LOCAL_REPOS` | off | `1` lets developers analyse server-local directories (`local_path`); self-hosted/dev only |
| `ARCHFLOW_CODEGRAPH_MAX_REPO_MB` / `_CLONE_TIMEOUT` | `500` / `180` | Clone size cap and time limit |
| `ARCHFLOW_CODEGRAPH_MAX_UPLOAD_MB` | `25` | Upload size limit |
| `ARCHFLOW_CODEGRAPH_JOBS` / `_PROCS` | `1` / `2` | Concurrent analyses / parser processes per analysis |

Security: only `https://` URLs without credentials on port 443 are accepted; the host is resolved and **every**
address must be public (loopback, private, link-local — including the cloud metadata address — CGNAT, multicast
and reserved ranges are refused), and git is pinned to the vetted address (`http.curloptResolve`) with redirects
off, so DNS rebinding cannot redirect the clone. Clones are shallow (`--depth 1`, no tags, submodules, hooks,
LFS or symlinks), killed at the time/size limit and deleted right after parsing. Parsing runs in a separate
worker process (`python -m archflow.codegraph.worker`), never in the API process, and repository code is never
executed. Only the normalized graph is stored (zlib-compressed, table `code_graphs`). Jobs run in an in-process
thread pool with an in-memory status registry — `codegraph/jobs.py` documents the seam for a durable queue.
Analysing, uploading and reconstructing need the developer role; viewers can read summaries, drift and impact.

### Running on Postgres

```bash
docker compose up -d postgres redis
DATABASE_URL=postgresql://archflow:<POSTGRES_PASSWORD>@localhost:5432/archflow uv run uvicorn archflow.main:app --port 8000
```

Migrations in `backend/archflow/migrations/` apply automatically on startup. Branch edits take a row lock
(`SELECT … FOR UPDATE`), so several API processes can run side by side.

### Accounts, roles and security

Everyone signs in with email and password (hashed with scrypt). The session is an opaque random token in an
`HttpOnly`, `SameSite=Lax` cookie (`Secure` when `ARCHFLOW_PUBLIC_URL` is https) with a 14-day sliding expiry;
only its SHA-256 is stored. Sign-in attempts are rate limited per email + IP (10 failures / 15 min, in memory;
`users.LoginLimiter` is the seam for a shared Redis limiter when running several API processes).

- **Bootstrap:** the first account becomes **owner of every existing org**, including `org-default`, so data from
  before accounts existed is claimed. Later accounts join through an invite link, or get their own
  "*name*'s workspace" (set `ARCHFLOW_OPEN_SIGNUP=false` to require invites).
- **Invites:** admins create single-use links (`#/invite/<token>`, valid 7 days, optionally bound to an email)
  under **Members & invites**; the invitee signs up or signs in to join with the chosen role.
- **Migration:** memberships used to be free-form display names; those rows are dropped on upgrade
  (`005_users.sql` / SQLite in-place migration), and API tokens created before accounts existed are revoked.

| Role | Can |
|---|---|
| viewer | read projects, versions, diffs, analysis, decisions and the member list; ask the AI questions; connect MCP agents read-only |
| reviewer | viewer + change a decision's **status** (accept / reject / supersede) |
| developer | edit the canvas, requirements, versions, branches; AI commands, design chat, convert, low-level design, decisions |
| architect | same as developer (reserved for per-project overrides) |
| admin | + members and roles (up to admin), invite links, personal access tokens and connected apps, org AI keys, delete projects |
| owner | + rename the org, grant/remove the owner role. An org always keeps at least one owner |

Every API route checks the caller's role in the project's org: `401` when not signed in, `404` for projects or
orgs you are not a member of, `403` when your role is too low. Activity and versions are always attributed to
the signed-in account (client-supplied `actor`/`author` fields are ignored). Public endpoints: `/api/health`,
`/api/catalog`, `/api/templates`, `/api/prompt-targets`, `/api/mcp-info`, sign-up/sign-in, and
`GET /api/invites/{token}` (requires the secret token). `/api/internal/*` uses the collab shared secret.

- **CSRF / CORS:** unsafe requests that carry the session cookie (and all sign-in/sign-up requests) must come
  from an allowed origin (`Origin`, else `Referer`): the origin of `ARCHFLOW_PUBLIC_URL` plus
  `ARCHFLOW_ALLOWED_ORIGINS` (default `http://localhost:5173`). CORS allows exactly those origins, with credentials.
- **Real-time:** the browser fetches a 2-minute HMAC-signed ticket (`GET /api/projects/{pid}/collab-ticket`,
  keyed with `ARCHFLOW_INTERNAL_TOKEN`) and presents it to the collab server, which checks signature, expiry
  and document and opens viewers/reviewers **read-only**. The `/ws` event socket requires the session cookie.
- **Org AI keys (BYOK):** admins can set Gemini / Anthropic / OpenAI keys per org (**AI keys** on the projects
  page). They are encrypted with AES-256-GCM using a key derived from `ARCHFLOW_SECRET_KEY` (required for this
  feature) and only the last 4 characters are ever shown. LLM calls for that org use its keys; the server's
  env keys remain the fallback for providers the org has not set.
- **Dev sign-in across :5173 and :8000:** the app reaches the API through the Vite proxy, so the cookie is set
  for `localhost`; browsers do not separate cookies by port, so the same session is valid on the API's own
  pages (the MCP consent page on :8000). If you are not signed in there, the consent page sends you to a
  minimal API-served sign-in form (`/oauth/login`) and back. In production serve the app and the API from
  the same site (e.g. one domain behind a reverse proxy) so the `SameSite=Lax` cookie is first-party.

### Connecting Claude and other agents over MCP

The API serves a **remote MCP server** at `http://localhost:8000/mcp` (Streamable HTTP), protected by
OAuth 2.1: dynamic client registration, PKCE, a consent page, rotating refresh tokens and revocation.
The consent page requires a signed-in ArchFlow account and lists only your organizations. Every connection
is bound to you and one organization, as read-only or read & edit, capped by your role (viewers and reviewers
can only grant read access). Each call re-checks your **current** membership and role: removed members lose
access immediately and demoted members become read-only. Open **⌘ Connect AI** in the app for copy-paste
instructions; admins also manage personal access tokens and connected apps there.

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
  store.py       SQLite persistence (store_pg.py: Postgres)
  users.py       passwords (scrypt), sessions, login rate limiting, trusted origins
  authz.py       roles, FastAPI auth dependencies, CSRF middleware
  accounts.py    sign-up/in, orgs, members, invites, org AI keys
  secrets_box.py AES-GCM encryption of org secrets
  main.py        REST + WebSocket API
  mcp_server.py  MCP server
  codegraph/     repository analysis: Graphify adapter (the only module importing it), worker process, CLI,
                 detections, sources (clone/SSRF guard), jobs, reconstruction, drift, impact, routes
frontend/src/
  components/Workspace.tsx   canvas, sync, orchestration
  components/*               nodes, palette, inspector, panels, command bar, dialogs
  realtime.ts                WebSocket hook
```

## Tests

```bash
cd backend && uv run pytest     # add TEST_DATABASE_URL=postgresql://… to also run the Postgres store tests
cd frontend && npm run build    # typecheck + production build
cd collab && npm test           # end-to-end: two live clients + MCP/AI edits (needs the API and collab server running)
```

## Roadmap

The full production plan (architecture, technology choices, phases) is in [docs/production-plan.md](docs/production-plan.md).

In rough order of leverage:

1. **ADRs** as first-class objects linked to components and requirements.
2. **Branch merge** with three-way diff and AI-assisted conflict resolution.
3. **Architecture alternatives + JEV evaluation**: generate options A/B/C as branches and score them.
4. **Comments, @mentions and approval workflows** on any object; per-project role overrides, SSO.
5. **Repository analysis**: done for public repos and uploaded graphs; next: GitHub App, drift on every push/PR, PR review comments.
6. **Jira sync**: epics/stories generated from requirements, with status reflected on the canvas.
7. **Diagram generation** from the same model: sequence, C4, ER, deployment, agent graph, MCP topology.
8. **Document intake**: SRS/PDF → requirements; meeting transcripts → live canvas.
9. **Test generation, IaC generation, and documentation (SRS/BRD/runbooks)**.
10. **Postgres + CRDT (Yjs) layer** for larger teams and offline editing; component registry & marketplace.

## License

Apache-2.0. The optional repository-analysis extra uses [Graphify](https://pypi.org/project/graphifyy/)
(`graphifyy`, Apache-2.0; ships an additional MIT license file and NOTICE) and its tree-sitter grammars; it is not installed by default.
