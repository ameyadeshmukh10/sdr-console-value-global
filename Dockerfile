# syntax=docker/dockerfile:1
#
# SDR Console — single-image deploy (Railway).
# The stack is a Vite/React frontend + a Python backend that serves BOTH the
# JSON API and the built SPA from one process. The backend is stdlib except for
# pyproject.toml/uv.lock (pymongo, dnspython — installed with uv). The committed data/
# ships as a first-boot seed for the Railway Volume mounted at /app/data (see
# docker-entrypoint.sh).

# ---- Stage 1: build the Vite/React frontend -> webui/frontend/dist ----
FROM node:22-slim AS frontend
WORKDIR /app/webui/frontend
# Install deps first (cached unless the lockfile changes), then build.
COPY webui/frontend/package.json webui/frontend/package-lock.json ./
RUN npm ci
COPY webui/frontend/ ./
RUN npm run build

# ---- Stage 2: resolve Python deps into /app/.venv with uv ----
FROM python:3.12-slim AS base

FROM base AS python-deps
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
# Compile bytecode; copy (not hardlink) so the venv can be lifted into the runtime stage.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Cached unless the lockfile changes. --frozen fails the build if uv.lock is stale.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ---- Stage 3: runtime (Python stdlib serves API + built SPA) ----
FROM base AS runtime
ENV PYTHONUNBUFFERED=1
WORKDIR /app
# Node 22 powers the Signal Playbook deck renderer (Vite single-file HTML build).
# No browser needed: the play publishes as a live HubSpot page, not a PDF.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
# Python deps (pymongo, dnspython) from the uv stage.
COPY --from=python-deps /app/.venv /app/.venv
# The venv's python3 shadows the system one (entrypoint + sys.executable subprocesses).
ENV PATH="/app/.venv/bin:$PATH"
# Renderer deps (cached unless its lockfile changes).
COPY deck-renderer/package.json deck-renderer/package-lock.json ./deck-renderer/
RUN npm ci --prefix deck-renderer --no-audit --no-fund
# Backend + .claude pipeline scripts + committed data + docs (see .dockerignore for exclusions).
COPY . .
# Bring in the built frontend from stage 1 (dist is gitignored, so it isn't in the context).
COPY --from=frontend /app/webui/frontend/dist ./webui/frontend/dist
# Stash the committed data as a one-time seed; /app/data is the live dir (a Railway Volume in
# prod). The entrypoint copies the seed into /app/data only when the Volume is empty.
RUN mv data seed-data && mkdir -p data && chmod +x docker-entrypoint.sh
# Documentation only — Railway injects $PORT and app.py binds 0.0.0.0:$PORT.
EXPOSE 8080
ENTRYPOINT ["./docker-entrypoint.sh"]
