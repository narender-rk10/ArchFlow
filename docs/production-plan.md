# ArchFlow production plan

> **Note: this is the production system, not the MVP.** The MVP in this repository proved the core idea:
> one structured model edited by humans, AI and MCP agents. This plan replaces its storage, realtime and
> AI layers and adds multi-tenancy, integrations, channels and meeting agents.

Status: draft for review · Owner: Narender · Date: 2026-10-01

---

## 1. Product scope

An organization signs up, creates projects, and invites users. Inside a project, people and agents work
on the same architecture at the same time, like Google Docs:

1. **Capture requirements** from web chat, Slack, Telegram, WhatsApp, or a Google Meet call where the
   ArchFlow agent listens, asks questions and updates the canvas live while sharing it on screen.
2. **Text-to-canvas plus manual editing**: the agent draws and edits the architecture; people drag, wire
   and edit the same canvas. Any component from any vendor (AWS, Azure, GCP, on-prem, SaaS, OSS, AI/agent).
3. **HLD and LLD**: high-level design (systems and containers) drills down into low-level design (modules,
   classes and functions, APIs, DB schema, sequences).
4. **Drafts and versions**: every architecture has drafts (branches), versions, diffs, merges and history.
5. **Decisions with JEV**: generate alternative architectures and evaluate them with the Jev decision API;
   record ADRs.
6. **Code**: connect GitHub, GitLab or Bitbucket, or a local directory; build a code knowledge graph;
   reconstruct the architecture; detect drift.
7. **Delivery**: Jira tickets with acceptance criteria, vibe-coding prompts as Markdown, agent skills,
   and hand-off to Claude Code or Codex.
8. **MCP everywhere**: everything the UI can do, an MCP client (Claude, Codex, Cursor…) can do.

Web first; mobile and desktop later.

---

## 2. System architecture

```text
 Clients                              Channels                         Agents (external)
 Web (React, PWA) ─┐                  Slack · Telegram · WhatsApp ─┐   Claude Code · Codex · Cursor ·
 Desktop/Mobile    │                  Google Meet bot ─────────────┤   Claude.ai connectors
 (later)           │                                               │          │ MCP (Streamable HTTP + OAuth)
                   ▼                                               ▼          ▼
            ┌─────────────┐    ┌────────────────────┐    ┌───────────────┐  ┌───────────────┐
            │  API (Fast- │◄──►│ Collab server      │    │ Channel       │  │ Remote MCP    │
            │  API)       │    │ (Hocuspocus + Yjs) │    │ gateway       │  │ server        │
            └──────┬──────┘    └─────────┬──────────┘    └───────┬───────┘  └───────┬───────┘
                   │  ops               │ Y.Doc updates          │                  │
                   ▼                    ▼                        ▼                  ▼
            ┌──────────────────────────────────────────────────────────────────────────┐
            │  Core domain service: operations · validation · versioning · projections │
            └──────┬───────────────────────┬────────────────────────┬──────────────────┘
                   ▼                       ▼                        ▼
             Postgres (+pgvector)      Redis                  Object storage (R2/S3/GCS)
             tenants, projections,     pub/sub, presence,     Yjs snapshots, uploads,
             op log, versions,         rate limits, queue     repo archives, exports
             code graph, search
                   ▲
            ┌──────┴──────────────────────────────────────────────────────────────┐
            │ Workers (queue): AI agent runs · repo analysis · Jira/Git sync ·     │
            │ JEV evaluation · meeting bot sessions · exports                      │
            └──────┬──────────────────────────────────────────────────────────────┘
                   ▼
            LLM gateway (LiteLLM Router): Gemini primary · Claude · OpenAI · retries,
            backoff, fallbacks, budgets · Langfuse tracing
```

### Principles carried over from the MVP
- **Single mutation pipeline.** Every change (human, AI, MCP, meeting agent, Git sync) is an operation that is
  validated, applied, logged with actor and reason, and broadcast. The MVP's `ops.py`, `analysis.py` and
  `vibe.py`, plus the MCP tool set, carry over.
- **Structured, never pictures.** Canvas objects are typed data with schemas.

---

## 3. Key technology decisions

### 3.1 Database: PostgreSQL (managed) plus pgvector

| Option | Verdict |
|---|---|
| **Postgres on Neon** | **Start here.** Cheapest at low/variable load (scale-to-zero, ~$19/mo entry), copy-on-write DB branches for previews/tests, pgvector, standard Postgres so no lock-in. |
| Supabase | Good if we want auth + storage + realtime bundled (~$25/mo). We are building our own realtime and auth, so less value. |
| Cloud SQL / AlloyDB / RDS | Move here when load is steady and predictable (cheaper per sustained vCPU, private networking with the rest of the stack). |
| CockroachDB / YugabyteDB | Only if we need active-active multi-region. Higher cost and complexity; not needed for a long time. |
| MongoDB / DynamoDB | No. We need transactions, joins and relational traceability. |
| Neo4j / graph DB | No for now. Traceability and code graph fit Postgres tables + recursive CTEs; revisit only for very large code graphs. |

Database cost is small next to LLM tokens and meeting-bot minutes. Pick the boring, portable option.

**Schema outline**
- Tenancy: `orgs`, `users`, `memberships(role)`, `projects`, `project_members`, `invites`, `api_tokens`.
  Every row carries `org_id`; Postgres **row-level security** enforces isolation.
- Model projection (queryable copy of each canvas): `diagrams(kind=HLD|LLD, parent_node_id)`,
  `nodes`, `edges`, `requirements`, `adrs`, `comments`, with JSONB `props`.
- History: append-only `operations(actor, source, reason, op jsonb, created_at)`; `drafts` (branches);
  `versions(snapshot_uri, op_seq)`.
- Code: `repos`, `code_files`, `code_symbols`, `code_edges(calls|imports|inherits)`, `infra_resources`.
- Integrations: `connections(provider, scopes, encrypted_tokens)`, `external_links(object ↔ jira/pr/commit)`.
- Search: Postgres full-text first; `embeddings` (pgvector) for semantic search and RAG.

### 3.2 Real-time collaboration: Yjs CRDT with a Hocuspocus server

Google-Docs-style editing needs a CRDT, not the MVP's last-write-plus-conflict-check.
- **Yjs** document per diagram per draft (nodes/edges/requirements as `Y.Map`s). Offline edits, undo/redo,
  and awareness (cursors, selections, presence) come for free.
- **Hocuspocus** (Node, MIT) as the self-hosted sync server. Its Redis extension enables multiple instances;
  its DB extension persists binary doc state. Self-hosting is cheaper at scale than Liveblocks.
- **Server-side edits** (AI, MCP, Git sync) go through the core service, which validates the op and applies it
  to the Y.Doc through a Hocuspocus direct connection. Every edit reaches clients the same way.
- **Projection**: on each debounced store, the collab server writes the normalized projection to Postgres
  so analysis, search, MCP reads and diffs never parse Yjs.
- **Conflicts**: CRDT merges automatically. For *semantic* fields (technology, type, protocol), we detect
  concurrent changes from different users within a window and show a "changed by X and Y, keep which?"
  notice. That keeps the MVP's conflict UX without blocking edits.

### 3.3 Canvas: React Flow (MIT) plus a component catalog

- React Flow stays: it is built for typed nodes and edges, which is what an architecture is. A freeform
  whiteboard mode can use Excalidraw (MIT) later. tldraw needs a commercial licence for production use,
  so verify before choosing it.
- **Component catalog as data**, not code. Each component type has:
  `id`, `vendor` (aws, azure, gcp, generic, oss, saas), `category` (compute, data, messaging, AI, security…),
  `abstract_type` (for example `managed-relational-db`), `icon`, `props_schema` (JSON Schema),
  `ports` (allowed connection kinds), `docs_url`.
- **Icons**: official sets. Azure (MIT-style terms for diagrams), GCP (Apache-2.0), AWS (CC-BY-ND 2.0:
  use unmodified, with attribution) via the thesvg/vendor packs, plus Simple Icons for OSS/SaaS.
  Vendor terms limit use to architecture diagrams, which is our use; legal check before launch.
- `abstract_type` enables **cloud-agnostic ↔ cloud-specific conversion**: "move this to Azure" maps
  RDS → Azure Database for PostgreSQL, SQS → Service Bus, and so on.
- Users and orgs can add custom component types (private catalog), and later a public registry.

### 3.4 HLD and LLD: the C4 model

- **HLD** = C4 levels 1–2 (system context, containers). **LLD** = levels 3–4 (components, code)
  plus API contracts (OpenAPI), DB schema (ERD), and sequence diagrams.
- Any HLD node can open its own LLD diagram (`diagrams.parent_node_id`); breadcrumbs navigate between levels.
- LLD can be generated from requirements (top-down) or from the code graph (bottom-up), and compared to
  detect drift.
- **Status (MVP):** each node has a `parent_id` ("" = HLD; otherwise the component whose diagram holds it)
  instead of a separate `diagrams` table; new node types `component`, `endpoint` (props `method`, `path`),
  `table` (props `columns`) and `event`. Ops (Python `ops.py` and the Yjs mirror `collab/src/ydoc.ts`) reject
  cross-diagram connections and delete a component's internals with it. `lld.py` generates the top-down LLD
  (LLM with JSON schema, template fallback) via `POST /api/projects/{pid}/nodes/{id}/lld` and MCP
  `generate_lld`; `GET …/nodes/{id}/views` derives the endpoint list/OpenAPI text, ER data and Mermaid
  sequence/ER diagrams. Bottom-up generation from code and drift detection are not built yet.

### 3.5 LLM layer: LiteLLM Router, Gemini primary

- **LiteLLM Router** (used as a library inside the API/workers, or as the LiteLLM proxy) with model groups:
  `reasoning` (Gemini Pro primary → Claude → GPT fallback), `fast` (Gemini Flash), `embedding`, `stt/tts`.
  Swapping providers is configuration, not code.
- **429 / rate-limit strategy**
  - Router: `num_retries`, `retry_after` honoring the `Retry-After` header, deployment **cooldowns**,
    `fallbacks` to another provider, multiple API keys/regions per model group for load balancing.
  - Our own wrapper as well: exponential backoff **with jitter** plus a circuit breaker. LiteLLM has a known
    gap where async *streaming* Gemini 429s skip fallback, so we catch and re-dispatch at our layer.
  - Token-bucket limits **per org** (Redis) so one tenant cannot exhaust shared quota; per-org monthly budgets.
  - Long jobs (generate architecture, analyze repo) run in **queued workers**, never in request threads;
    clients see progress over WebSocket.
- **Structured output everywhere**: agent tools = the same operation schema as MCP, validated with Pydantic
  before applying. Models never write the canvas directly.
- **Observability**: Langfuse (self-hostable) for traces, cost per org, prompt versions and evals.
- BYOK option: an org can bring its own Gemini/OpenAI/Anthropic keys.

### 3.6 Agent design

One **ArchFlow agent** with a single tool registry, exposed three ways: internal function calling, remote
MCP, and channel bots. Tool groups:
- model: `get_architecture`, `add/update/remove_component`, `connect/disconnect`, `requirements.*`, `adr.*`
- structure: `create_lld(node)`, `generate_sequence`, `convert_cloud(target)`, `layout`
- quality: `validate`, `review`, `diff`, `explain_change`
- decisions: `generate_alternatives`, `evaluate_with_jev`
- delivery: `create_jira_tickets`, `generate_vibe_prompt`, `generate_skill`, `open_pr`, `dispatch_coding_agent`
- code: `analyze_repo`, `search_code_graph`, `detect_drift`

Sub-flows (requirements interviewer, architecture generator, reviewer, meeting facilitator) are prompts
plus tool subsets on the same agent, not separate systems.

### 3.7 MCP: a remote server

- **Remote MCP server** over Streamable HTTP with **OAuth 2.1** (MCP authorization spec), scoped per
  org/project and role. Works as a Claude.ai connector, in Claude Code (`claude mcp add --transport http`),
  Codex, Cursor and others.
- Keep the stdio server for local or offline use.
- Additional MCP tools for coding agents: `list_tasks`, `get_task_brief`, `get_acceptance_criteria`,
  `report_progress`, `link_pr`. A developer's Claude Code can pull the next ArchFlow task, implement it,
  and report back.

### 3.8 JEV decision layer

- Jev API: `POST https://thejevai.com/v1/systemone` with `state`, `model` (`jev-latest`), and `questions`
  of type `choice`, `score` or `noul`. Bearer auth; 429/529 need backoff.
  *(Confirm this is the "JEV" you mean; the user-facing name was "jev.ai".)*
- Flow: the agent generates 2–4 alternatives (each a draft) → we build `state` = requirements + NFRs +
  each alternative's model summary → `questions`:
  - `choice` "Which alternative best satisfies the requirements?" (options = alternatives)
  - `score` per criterion: cost, scalability, latency, security, operability, delivery time, team fit
  - `noul` guards: "Does alternative B meet the 99.9 % availability NFR?"
- Results become a comparison matrix on screen plus an **ADR draft** linked to the chosen draft and to the
  requirements that drove it. A human approves; the chosen draft merges to main.

### 3.9 Code integrations

- **GitHub App** (not personal tokens): per-repo install, fine-grained permissions, webhooks for push/PR.
  **GitLab** and **Bitbucket Cloud** via OAuth apps and webhooks.
- **Repo analysis worker** (sandboxed, ephemeral): shallow clone → tree-sitter parsing → code graph
  (files, symbols, calls, imports) → infra parsing (Terraform, Kubernetes, Docker Compose, Helm, serverless,
  CI files) → reconstructed HLD/LLD → stored in Postgres → repo archive deleted.
- **Local directories**: an `archflow` CLI (also an MCP server) parses on the developer's machine and
  uploads only the graph, so source never leaves the machine. It can also import graphs from existing tools
  (Understand-Anything, Graphify, CodeGraph) rather than competing with them.
- **Drift detection**: designed model vs code-derived model diff, on every push and PR; PR check comments.
- *Status (MVP):* implemented with Graphify as the parser (`backend/archflow/codegraph/`, optional extra
  `codegraph`): public-URL shallow clones and server-local dirs (opt-in) analysed in a separate worker process,
  `archflow-codegraph` CLI + graph upload, normalized graphs in `code_graphs`, reconstruction to a draft branch,
  drift in the analysis, change impact, REST + MCP tools. Still to do: GitHub App/webhooks, a durable job queue,
  drift on push/PR with check comments, Helm/serverless/CI parsing.

### 3.10 Delivery: Jira, prompts, skills, coding agents

- **Jira Cloud** (OAuth 2.0 3LO, REST v3): epics from capabilities, stories from requirements, acceptance
  criteria as Given/When/Then, a link back to ArchFlow objects; webhook sync of status. Linear later.
- **Vibe-coding pack** (Markdown), committed via PR to `.archflow/` or downloaded:
  `AGENTS.md` / `CLAUDE.md` (project context + conventions), `tasks/<id>.md` (implementation brief per
  story), and `skills/<name>/SKILL.md` (reusable agent skills such as "add a service following our
  architecture").
- **Hand-off to coding agents**, three levels:
  1. *Pull (default)*: the developer's own Claude Code/Codex uses the ArchFlow MCP to fetch tasks. No
     execution cost or code-execution risk on our side.
  2. *PR-triggered*: open an issue/PR with the brief; **Claude Code GitHub Actions** (`@claude`) or Codex's
     GitHub integration implements it in the customer's own CI.
  3. *Managed execution (paid, later)*: our sandbox workers run the **Claude Agent SDK** or **Codex SDK**
     headless against the repo (customer's API key) and open a PR. Strong isolation needed.

### 3.11 Channels

- **Slack** (Bolt, Socket Mode or Events API), **Telegram** (Bot API webhooks), **WhatsApp** (Cloud API:
  Meta business verification, 24-hour session window, templates for proactive messages). One channel
  gateway normalizes messages → agent → replies, with canvas snapshots rendered as images plus deep links.
- Identity linking: a channel user maps to an ArchFlow user and project via a one-time link flow.

### 3.12 Google Meet agent

- Google has no official API for a bot to speak or present, and the Meet Media API is still Developer
  Preview (every participant must be enrolled). So a meeting agent joins as a participant from a headless
  browser.
- **Recommended: Recall.ai** (or Meeting BaaS) for joining, real-time transcript/audio, and outputting audio
  and a webpage as the bot's video. We render the **live canvas URL** as the bot's video feed, which works as
  screen sharing. Self-hosting a Playwright bot is cheaper per hour but costly to keep working; revisit at scale.
- Pipeline: real-time transcript → requirement extractor (fast model) → agent decides: update the model,
  ask a question (TTS into the meeting), or note an action item → ops applied to the live canvas everyone sees.
- Guardrails: a "listening only" mode; changes during a meeting go to a **meeting draft** that the host
  merges; consent notice on join.

### 3.13 Tenancy, auth, permissions

- Org → projects → members. Roles: Owner, Admin, Architect, Developer, Reviewer, Viewer; per-project overrides.
- **Implemented (v1):** email + password accounts (scrypt), cookie sessions, org roles enforced on every API route,
  invite links, CSRF/CORS origin checks, signed collab tickets (read-only for viewers/reviewers), MCP consent bound
  to the signed-in user with role-capped scopes, per-org encrypted AI keys. See README "Accounts, roles and security".
- Next: SSO via **WorkOS AuthKit** (generous free tier, enterprise SSO/SCIM when needed) or self-hosted Keycloak
  if we want zero vendor cost. Postgres RLS on `org_id` for defense in depth.
- Audit log of every operation, plus approvals (draft → review → approved) per object type.

### 3.14 Infrastructure (cheap first, scalable later)

| Concern | Start | Scale |
|---|---|---|
| API, workers, MCP, channel gateway | Containers on **Cloud Run** (GCP, near Gemini) | GKE/Kubernetes when we need fine control |
| Collab server (WebSockets) | Cloud Run with session affinity + Hocuspocus Redis extension | Dedicated node pool |
| Postgres | Neon | Cloud SQL / AlloyDB |
| Redis | Upstash or Memorystore | Memorystore cluster |
| Object storage | Cloudflare R2 (no egress fees) or GCS | same |
| Queue | Redis-backed (arq/Dramatiq) | Temporal for long multi-step agent workflows |
| Observability | OpenTelemetry → Grafana Cloud free tier; Langfuse; Sentry | same, paid tiers |
| Secrets | GCP Secret Manager + KMS envelope encryption for OAuth tokens | same |

Everything containerized; `docker compose up` gives the full stack locally.

---

## 4. Delivery phases

Each phase ends deployable.

| Phase | Scope | Exit criteria |
|---|---|---|
| **P0 Foundation** | Monorepo, Docker Compose, Postgres + RLS + Alembic, auth & orgs/projects/invites, Redis, LiteLLM router with retry/backoff/budgets, Langfuse, CI/CD | Two orgs isolated; LLM 429 chaos test passes |
| **P1 Collaborative canvas** | Yjs + Hocuspocus, React Flow on Yjs, presence/cursors, component catalog (AWS/Azure/GCP/generic) with icons, HLD→LLD drill-down, comments/mentions | 10 users edit one diagram smoothly; offline edit merges |
| **P2 Agent + MCP** | Text-to-canvas agent on the core op pipeline, requirements interviewer, review/validation, remote MCP with OAuth, Claude.ai/Claude Code connection | Claude Code edits a diagram live via remote MCP |
| **P3 Drafts, versions, decisions** | Drafts/branches, versions, visual diff, merge, ADRs, alternatives + JEV evaluation | Pick between 3 generated alternatives with a JEV matrix → ADR → merge |
| **P4 Code & delivery** | GitHub App (+GitLab/Bitbucket), repo → code graph → HLD/LLD, local CLI, drift detection, Jira sync, vibe pack (prompts, AGENTS.md, skills) via PR, Claude Code Action/Codex hand-off | Requirement → Jira story → PR implemented by Claude Code, traced back |
| **P5 Channels & meetings** | Slack, Telegram, WhatsApp, Google Meet agent (listen → live canvas → speak → present canvas) | Live meeting changes the architecture on screen |
| **P6 Scale & reach** | Mobile/desktop (PWA → Tauri/Capacitor), public component registry, managed coding execution, enterprise SSO/SCIM, usage billing | — |

### What we keep from the MVP
`ops.py` (operation semantics), `analysis.py`, `diff.py`, `vibe.py`, the MCP tool surface, and the React
components (nodes, palette, panels). Replaced: SQLite store, in-memory WebSocket hub, single-provider AI module.

---

## 5. Risks

| Risk | Mitigation |
|---|---|
| LLM cost per org | Fast model for extraction, reasoning model only for edits; caching; per-org budgets; BYOK |
| Gemini rate limits | Multiple keys/regions, cooldowns, cross-provider fallbacks, queueing |
| Meeting bot fragility (Meet UI changes) | Managed provider (Recall.ai) absorbs it |
| Customer code security | GitHub App least privilege, ephemeral sandboxes, local CLI option, no code retained |
| CRDT and validation mismatch | All non-human edits go through the op pipeline; projection validator flags invalid states |
| Icon licensing | Use official sets unmodified with attribution; legal review |
| Scope | Strict phase gates; each phase ships |

---

## 6. Open questions

1. "GME" → **Gemini**? Which tier is the default (Pro for reasoning, Flash for fast paths)?
2. "Bucket" → **Bitbucket**, or cloud storage buckets (S3/GCS/Azure Blob) for reading IaC and config?
3. JEV → is it the Jev decision API at thejevai.com? Do we have an API key, partnership or volume pricing?
4. Cloud and region preference (GCP fits Gemini), data-residency needs, and monthly budget.
5. Meeting agent: managed Recall.ai (fast, per-hour cost) vs self-hosted bot?
6. Coding hand-off: pull via MCP + PR first, or do we need managed execution early?
7. Enterprise SSO needed at launch?
8. Team size and target date for P0–P2.
