# Deploying ArchFlow

## What runs

| Service | Image | Port | Notes |
|---|---|---|---|
| `web` | `frontend/Dockerfile` (build context: repo root) | 8080 | nginx: the app, plus routing of `/api`, `/ws`, `/collab`, `/mcp` and the OAuth paths |
| `api` | `backend/Dockerfile` | 8000 | FastAPI, one worker. Includes Node.js (diagram export, OpenSpec CLI) and git (repository analysis) |
| `collab` | `collab/Dockerfile` | 1234 | Hocuspocus real-time server; talks to the API with `ARCHFLOW_INTERNAL_TOKEN` |
| `postgres` | `pgvector/pgvector:pg16` | 5432 | Schema is created and upgraded by the API at start (SQLAlchemy models + Alembic, `archflow/db/`); databases from earlier versions are converged in place |

Only `web` needs to be reachable from outside. Put TLS in front of it (a load balancer, Caddy, Cloudflare).

## First deploy

```bash
cp .env.example .env
# required
#   POSTGRES_PASSWORD=<random>          python -c "import secrets; print(secrets.token_urlsafe(32))"
#   ARCHFLOW_INTERNAL_TOKEN=<random>
#   ARCHFLOW_SECRET_KEY=<random>         encrypts organizations' own AI keys
#   ARCHFLOW_PUBLIC_URL=https://archflow.example.com
#   ARCHFLOW_MCP_ALLOWED_HOSTS=archflow.example.com
#   GEMINI_API_KEY=...                   (or ANTHROPIC_API_KEY / OPENAI_API_KEY)
docker compose -f docker-compose.prod.yml up -d --build
```

Open the public URL and sign up: the first account owns every organization. Further sign-ups need an invite link
unless `ARCHFLOW_OPEN_SIGNUP=true`.

Useful build options: `ARCHFLOW_BUILD_EXTRAS=` (empty) leaves repository analysis out of the API image (about 200 MB
smaller); `ARCHFLOW_IMAGE_PREFIX` / `ARCHFLOW_VERSION` name the images for a registry.

## AI spend

Every LLM call is recorded with its tokens and an estimated cost (organization, project, feature, model). Org admins
see it under **AI usage** on the projects page and can set a **monthly budget**: once this month's spend reaches it,
AI features pause (falling back to their built-in behaviour) until the budget is raised or the month ends.
Estimates use LiteLLM's price list, else built-in prices; override with
`ARCHFLOW_LLM_PRICES='{"gemini-3.1-pro": [2.0, 12.0]}'` (USD per million input and output tokens).

## Operations

- **Backups**: back up the `pgdata` volume (or use a managed Postgres with point-in-time recovery).
- **Health**: `GET /healthz` (web), `GET /api/health` (API liveness; also reports LLM and export availability), `GET /api/ready` (readiness: the database answers and the schema is at the latest migration; 503 otherwise).
- **Upgrades**: `git pull && docker compose -f docker-compose.prod.yml up -d --build`; Alembic migrations run on start (serialized across API processes with a Postgres advisory lock). Check with `GET /api/ready` or `docker compose exec api alembic -c archflow/db/alembic.ini current`.
- **Logs**: `docker compose -f docker-compose.prod.yml logs -f api` (prompts and keys are never logged).

## Current limits (single instance)

Run exactly one `api` replica. Live updates (`/ws`), background jobs (OpenSpec generation, repository analysis),
the login limiter and the AI rate limiter keep state in the API process, so a restart cancels running jobs and two
replicas would not share events. Moving these to Redis is the next step for horizontal scaling.

## CI

`.github/workflows/ci.yml` runs on pushes to `main`/`develop` and on pull requests:
backend tests on SQLite **and Postgres**, the web app typecheck and build (plus an npm audit for high/critical
issues), a two-editor collaboration end-to-end test against a running API and collab server, and builds the three
images, starting the API image and checking that diagram export and OpenSpec work inside it.
