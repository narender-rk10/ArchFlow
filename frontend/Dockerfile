# ArchFlow web app, served by nginx, which also routes the API, collab and MCP paths (deploy/nginx.conf).
# Build from the repository root (the app shares the Yjs document model in collab/src):
#   docker build -f frontend/Dockerfile -t archflow-web .
FROM node:22-bookworm-slim AS build
WORKDIR /src
COPY collab/package.json collab/package-lock.json collab/
RUN cd collab && npm ci --no-audit --no-fund
COPY frontend/package.json frontend/package-lock.json frontend/
RUN cd frontend && npm ci --no-audit --no-fund
COPY collab/tsconfig.json collab/
COPY collab/src collab/src
COPY frontend frontend
RUN cd frontend && npm run build

FROM nginx:1.27-alpine
COPY deploy/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /src/frontend/dist /usr/share/nginx/html
# Upstreams (override for other topologies); nginx substitutes them into the template at start.
ENV API_UPSTREAM=http://api:8000 COLLAB_UPSTREAM=http://collab:1234
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
