# ArchFlow collaboration server (Hocuspocus + Yjs).
#   docker build -t archflow-collab collab
FROM node:22-bookworm-slim
ENV NODE_ENV=production
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force
COPY tsconfig.json ./
COPY src ./src
USER node
EXPOSE 1234
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:' + (process.env.COLLAB_PORT || 1234)).then(() => process.exit(0), () => process.exit(1))"
CMD ["node", "--import", "tsx", "src/server.ts"]
