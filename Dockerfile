# syntax=docker/dockerfile:1

# Pinned to the exact Python version this project is developed and
# tested against (3.12.10) rather than a floating "3.12-slim" tag, so
# a rebuild next month can't silently land on a different interpreter
# patch version.
FROM python:3.12.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so this layer is cached and only reinstalled when
# requirements.txt actually changes - rebuilding after an unrelated app/
# code change stays fast.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only copy what's needed to run the app - never the whole build
# context. tests/, evals/, .git/, venv/, tasks.db, .env, .env.docker are
# never copied, regardless of .dockerignore correctness (defense in
# depth on top of .dockerignore, which is the primary guard).
COPY app/ ./app/
COPY docker/healthcheck.py ./docker/healthcheck.py

# Dedicated non-root user. App code under /app stays root-owned and
# read-only for appuser (it only needs read+execute, which the default
# COPY permissions above already provide). Only /data - where the
# SQLite database lives - needs to be writable, so that's the only
# path chown'd here.
#
# This ordering matters for the named volume compose.yaml mounts at
# /data: Docker copies a path's existing content and permissions from
# the image into a brand-new named volume the first time it's
# attached, so an empty "agent_data" volume inherits this
# appuser-owned, empty /data directory on the very first
# `docker compose up` - no permission errors, no manual chown needed
# on the host.
RUN useradd --system --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data

# Default SQLite location inside the container - matches the named
# volume mounted at /data in compose.yaml. This is NOT the developer's
# real ./tasks.db on the host: that file is never copied into the
# image, and this path only exists inside the container/volume.
ENV DATABASE_URL=sqlite:////data/tasks.db

EXPOSE 8000

USER appuser

# GET /health is deliberately public (no X-API-Key required) - see
# app/main.py and app/services/auth.py. start-period is 10s (not the
# default 0s/5s) because a cold container's init_db() + the legacy
# user_id migration + importing anthropic/sqlalchemy can take a few
# seconds longer than a bare "hello world" app, and a false
# "unhealthy" flip during that window is exactly what start-period
# exists to prevent.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "docker/healthcheck.py"]

# Exec form (no shell) so SIGTERM reaches uvicorn directly as PID 1,
# for a clean FastAPI lifespan shutdown instead of a forced SIGKILL
# after the grace period. Exactly one worker, always: conversation/
# clarification/confirmation state (app/services/conversation_memory.py)
# is persisted in the same SQLite database as tasks/traces (the
# ConversationState table), so it now survives container restarts on
# the same volume - but one worker is still the only configuration this
# has been built and tested against. Multiple uvicorn workers sharing
# one SQLite file raises its own locking/concurrency questions that
# haven't been validated here, and the atomic, single-use confirmation-
# consumption logic (conversation_memory.consume_confirmation) has only
# been verified under a single worker process. Do not raise --workers
# without first validating that.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
