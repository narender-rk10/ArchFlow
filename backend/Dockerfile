# ArchFlow API (FastAPI). Build from the repository root or from backend/:
#   docker build -t archflow-api backend
#
# Includes Node.js (diagram renderer in renderer/, OpenSpec CLI in openspec/) and git (repository analysis).
# Runs one Uvicorn worker: live updates, background jobs and rate limiters are in-process today.

ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=22

# ---- OpenSpec CLI (npm dependency, no dev tools in the final image) ----
FROM node:${NODE_VERSION}-bookworm-slim AS openspec
WORKDIR /openspec
COPY openspec/package.json openspec/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force

# ---- Python dependencies ----
FROM python:${PYTHON_VERSION}-slim-bookworm AS deps
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
# EXTRAS="codegraph" adds repository analysis (Graphify + tree-sitter grammars, ~200 MB). Empty to leave it out.
ARG EXTRAS="codegraph"
RUN uv sync --frozen --no-dev --no-install-project $(for e in $EXTRAS; do printf -- '--extra %s ' "$e"; done)

# ---- Runtime ----
FROM python:${PYTHON_VERSION}-slim-bookworm
ARG NODE_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 archflow
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
COPY --chown=archflow:archflow archflow ./archflow
COPY --chown=archflow:archflow renderer ./renderer
COPY --from=openspec --chown=archflow:archflow /openspec ./openspec
RUN mkdir -p /app/data && chown archflow:archflow /app/data
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    ARCHFLOW_DB=/app/data/archflow.db \
    OPENSPEC_TELEMETRY=0 DO_NOT_TRACK=1 OPENSPEC_NO_UPDATE_CHECK=1
USER archflow
EXPOSE 8000
VOLUME ["/app/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "archflow.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-keep-alive", "75"]
