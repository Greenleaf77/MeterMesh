# syntax=docker/dockerfile:1.7

# ---- Stage 1: build the Vite frontend ----
FROM node:24-bookworm-slim AS frontend-builder

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci

COPY . .
RUN npm run build

# ---- Stage 2: runtime ----
FROM python:3.12-slim AS runtime

# No CODEX_USAGE_DB / CLAUDE_PROJECTS_DIR / OPENCODE_USAGE_DB defaults here on
# purpose. Sources are bind-mounted at their host-absolute paths, which are not
# knowable at build time, and baking in a fixed guess would shadow the app's own
# $HOME-relative defaults with a path nothing is mounted at -- indexing empty
# instead of failing loudly. docker-compose.yml sets all three explicitly.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    METERMESH_UNIBASE_DB=/data/unibase/unibase.sqlite3 \
    METERMESH_ALLOW_REMOTE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata bash zstd \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 metermesh \
 && useradd  --system --uid 10001 --gid metermesh \
             --home-dir /home/metermesh --shell /usr/sbin/nologin metermesh

WORKDIR /app

COPY --from=frontend-builder /app/dist ./dist
COPY dashboard_api.py unibase.py codex_usage.py claude_usage.py opencode_usage.py ./
COPY docker/ ./docker/

# A fresh named volume inherits its owner and mode from the image directory it
# shadows, so /data/unibase must stay group-writable: compose lets the operator
# run as an arbitrary uid (METERMESH_UID) to reach 0700 host sources, and that
# uid reaches the volume through the metermesh group rather than by owning it.
RUN chmod +x docker/entrypoint.sh \
 && mkdir -p /data/unibase \
 && chown -R metermesh:metermesh /app /data \
 && chmod 0775 /data/unibase

USER metermesh

EXPOSE 8765

ENTRYPOINT ["/app/docker/entrypoint.sh"]
