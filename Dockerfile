# syntax=docker/dockerfile:1
#
# Two build targets in one file, both built from the repo root as context:
#
#   docker build --target backend  -t trader-backend  .
#   docker build --target frontend -t trader-frontend .
#
# docker-compose.yml selects the targets for you.
#
# No secrets are baked in. API keys are supplied at RUN time via environment
# variables / an .env file, never via ARG or COPY of a .env.

# ------------------------------------------------------------------------------
# Backend - FastAPI + uvicorn
# ------------------------------------------------------------------------------
# The host machine runs Python 3.14; nothing in the source uses 3.13/3.14-only
# syntax, so 3.12-slim is a safe, well-supported base with prebuilt wheels for
# pandas/numpy.
FROM python:3.12-slim AS backend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl is only for the compose healthcheck; build-essential is not needed because
# every pinned dependency ships manylinux wheels for cp312.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first so source edits do not bust the layer cache.
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./

# Trade history lives on a mounted volume so it survives image rebuilds.
#
# portfolio.py currently hard-codes <backend>/portfolio.json, so /app/portfolio.json
# is symlinked onto the volume. Python's open(path, "w") follows symlinks, so writes
# land on /data. Once config.settings.portfolio_file is wired into portfolio.py the
# PORTFOLIO_FILE env var below takes over and the symlink becomes redundant (but
# harmless - it points at the same file).
ENV PORTFOLIO_FILE=/data/portfolio.json
RUN mkdir -p /data && ln -sf /data/portfolio.json /app/portfolio.json

# Run as a non-root user; /data must be writable by it.
RUN useradd --create-home --uid 10001 trader && chown -R trader:trader /app /data
USER trader

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]


# ------------------------------------------------------------------------------
# Frontend - Next.js 14 (build stage)
# ------------------------------------------------------------------------------
FROM node:20-alpine AS frontend-build

WORKDIR /app

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./

# NEXT_PUBLIC_* is inlined at build time. It is a public URL, not a secret.
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_TELEMETRY_DISABLED=1

RUN npm run build


# ------------------------------------------------------------------------------
# Frontend - runtime
# ------------------------------------------------------------------------------
FROM node:20-alpine AS frontend

WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

COPY --from=frontend-build --chown=node:node /app ./

USER node
EXPOSE 3000
CMD ["npm", "run", "start"]
