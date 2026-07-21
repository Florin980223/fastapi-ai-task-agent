# Deployment runbook

This document covers what's needed to run this app as a real, public-facing
deployment, on top of the local/dev setup already covered in the main
[README.md](../README.md). It is deliberately **platform-neutral** - it does
not assume Render, Railway, Fly.io, AWS, Azure, GCP, Kubernetes, or any other
specific hosting provider or orchestrator. Wherever platform-specific choices
are needed (a process manager, a reverse proxy, TLS termination, secret
storage), this document describes the requirement and lets you fill in the
concrete tool.

## Required environment variables

Copy [`.env.deploy.example`](../.env.deploy.example) and fill in real values -
it documents every variable, marked required vs. optional, with the same
placeholders-only convention as `.env.example`/`.env.docker.example`. Never
commit the filled-in copy.

The two variables that matter most for a real deployment, beyond what local
development needs:

- **`API_KEYS`** - required. The app refuses to start without at least one
  valid `key:user_id` pair. Generate real random keys
  (`python -c "import secrets; print(secrets.token_urlsafe(32))"`), not the
  `devkey-alice`/`devkey-bob` placeholders from the dev/demo examples.
- **`DATABASE_URL`** - required. See "PostgreSQL setup" below for why
  PostgreSQL, not SQLite, is the recommended choice here.

Every environment variable this app reads is validated at import time
(`app/config.py`) - a missing or malformed required value makes the process
exit immediately with a clear, actionable error message on stderr, and **that
message never includes the raw invalid value** for anything secret-shaped
(e.g. an `API_KEYS` parsing error only ever mentions an entry's position and
its `user_id`, never the key itself). There is no "start anyway with a
default" fallback for anything security-relevant.

## PostgreSQL setup (recommended for a public deployment)

SQLite remains fully supported and is still the right choice for local
development - zero setup, one file. For a public deployment, PostgreSQL is
recommended instead: concurrent access without file-locking contention, real
backup/restore tooling (`pg_dump`/`pg_restore`), and no single-file-on-disk
availability risk.

1. Provision a PostgreSQL 16 instance (any host works - this app only needs a
   reachable `postgresql+psycopg://` URL; provisioning itself is out of scope
   here, deliberately, to stay platform-neutral).
2. Set `DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>:<port>/<database>`.
3. Run the explicit migration command below **before** starting the app for
   the first time.
4. See `compose.yaml`'s `postgres` service and README.md's "PostgreSQL
   (optional)" section for a fully local, free way to exercise this same
   setup before deploying anywhere for real.

## Alembic migrations - explicit, never automatic

```
python -m alembic upgrade head
```

Run this by hand (or as an explicit, separate deploy-pipeline step) against
the target database **before** starting/restarting the application. This
project's application code **never** runs a migration automatically - not on
startup, not from `GET /ready`, not anywhere. `app/database.py`'s `init_db()`
(called from the FastAPI lifespan on every startup) and
`app/services/readiness.py`'s `check_ready()` (called on every `GET /ready`)
both only ever call `ensure_schema_is_current()`
(`app/services/schema_migration.py`), which **verifies** the schema is at
Alembic head and raises - never creates, alters, or stamps anything - if it
isn't. See "Health vs. readiness" below for exactly what happens when it
raises at each of those two points.

**Always back up the database before running `alembic upgrade head`** against
a real deployment's database (see "Backup before migration" below) - this is
what makes a real rollback possible later, since Alembic's own `downgrade` is
intentionally blocked at this project's baseline migration (see "Rollback
procedure").

If you're adopting an existing, pre-Alembic SQLite database (e.g. migrating
an old local demo into a real deployment), see README.md's "Database
migrations (Alembic)" section and `app/services/schema_migration.py`'s
`adopt-legacy` CLI subcommand first - `alembic upgrade head` alone is not the
right first step for a database Alembic has never touched.

## Startup order

1. Database is reachable and at Alembic head (`python -m alembic upgrade
   head`, see above - do this first, always).
2. Start the application container/process (`uvicorn app.main:app --host
   0.0.0.0 --port 8000 --workers 1` - see "Production server command"
   below). Its FastAPI lifespan runs `init_db()` before accepting any
   traffic; if the schema isn't at head (step 1 was skipped or failed), the
   process exits immediately rather than serving with a stale/invalid
   schema - see "Health vs. readiness" for exactly what that means for
   `/health`.
3. Wait for `GET /ready` to return `200` before routing real traffic to this
   instance (see the next section, and the CI `deploy_smoke` job for a
   concrete bounded-polling example).

## Health vs. readiness

Two separate endpoints, two separate purposes - do not conflate them:

- **`GET /health`** - liveness only. Returns `{"status": "ok"}`, always
  `200` if the process is alive and its event loop is responsive. Never
  touches the database. No auth required.
- **`GET /ready`** - readiness. Verifies database connectivity, the Alembic
  schema revision, and required configuration availability, all within a
  small bounded timeout (`READY_CHECK_TIMEOUT_SECONDS`, default `2.0`s), and
  never modifies the database. Returns `200 {"status": "ready"}` when ready,
  `503 {"status": "not_ready", "reason": "<fixed safe reason>"}` otherwise.
  No auth required (an orchestrator/load balancer probe typically can't
  supply an API key). The `reason` field is always one of a small fixed set
  of safe strings (`schema_not_adopted`, `schema_out_of_date`,
  `database_unavailable`, `config_unavailable`, `check_in_progress`,
  `timeout`) - **never** a database URL, a credential, or raw exception text.
  Server-side logs don't get the raw exception either - only this same
  fixed `event`/`reason` pair is ever logged (`app/services/readiness.py`
  deliberately never uses `logger.exception()`/`exc_info=True`, since a
  database driver's own error text can embed the connection string).

**The precise relationship between the two, including the case that trips
people up:**

- **Startup-time failure**: if the database/schema is invalid when the
  FastAPI lifespan runs `init_db()` at process start, that exception
  propagates uncaught, uvicorn's startup sequence fails, and the application
  **never starts serving requests at all** - there is no partially-running
  process where `/health` answers `200` in this case. `/health` is not
  "independently available" here; it is simply unreachable, because nothing
  is listening. This is intentional, correct, fail-closed behavior.
- **Post-startup failure**: once startup has succeeded once, if the database
  *later* becomes unreachable (a network blip, a Postgres restart, etc.),
  `/health` keeps returning `200` (it only proves the process is alive),
  while `/ready` correctly starts returning `503`. **This** is what
  "liveness independent of readiness" means in this project - only this
  case, never the startup-time one above.

Docker's own `HEALTHCHECK` (see the Dockerfile) intentionally stays pointed
at `/health`, not `/ready` - it drives `restart: unless-stopped`, and pointing
that at a DB-dependent check would restart-loop the container during a
transient database blip instead of just waiting. `/ready` is for an external
load balancer's or orchestrator's own readiness gate (and for a deploy
pipeline's "wait until actually ready" step, as in the CI `deploy_smoke` job)
- something with its own concept of "not yet ready" vs. "should be killed and
replaced," which Docker's single `HEALTHCHECK` mechanism doesn't have.

## Single-worker limitation

The production server command is, and must remain:

```
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

This is load-bearing, not a tuning knob: the in-memory rate limiter
(`app/services/rate_limiter.py`) keeps per-user counters in a process-local
dict, and `app/services/conversation_memory.py`'s confirmation-consumption
logic relies on single-process atomicity. Raising `--workers` above `1`
without first replacing both of those is unsafe and unsupported by this
codebase as it stands - this feature does not change that. If you need more
throughput, scale by running multiple independent single-worker instances
behind a load balancer instead (each with its own rate-limiter state - be
aware the effective per-user limit becomes N times the configured value
across N instances, exactly as it would with N workers).

## Proxy and HTTPS assumptions

This app expects to sit behind a reverse proxy that terminates HTTPS and
forwards plain HTTP to it - it has no TLS/HTTPS support of its own, by
design, to stay platform-neutral. Nothing in the codebase reads
`X-Forwarded-*` headers today, and nothing uses client IP for any decision
(the rate limiter keys on authenticated `user_id`, not IP) - this is a
safe-by-omission default, not a gap.

If you want uvicorn to honor `X-Forwarded-For`/`X-Forwarded-Proto` (e.g. for
accurate scheme detection in logs), pass `--forwarded-allow-ips` set to the
**exact** IP address of your trusted reverse proxy - **never** a wildcard
(`*`), and only when a proxy you actually trust is in front. TLS certificate
management, the reverse proxy itself, and its configuration are the
deployer's responsibility and are out of scope for this app.

## Docker hardening

Non-root user, exec-form `CMD` (clean `SIGTERM`/graceful-shutdown handling),
a container `HEALTHCHECK`, no baked-in secrets, and a minimal slim image are
already built into the `Dockerfile` - see it directly for details, and
README.md's "Docker" section for the local dev/demo workflow this all sits
on top of.

Beyond what's already there, consider layering on the following at run time
for a real deployment (documentation only here - not applied to the default
`compose.yaml`, which stays a local dev/demo workflow):

```
docker run \
  --read-only \
  --tmpfs /tmp \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  -v agent_data:/data \
  ...
```

- `--read-only` + `--tmpfs /tmp`: the application code under `/app` never
  needs to be writable at runtime - only `/data` (the SQLite volume mount
  point, irrelevant if you're using PostgreSQL) does. A `tmpfs` mount at
  `/tmp` covers anything that might need scratch space without making the
  whole filesystem writable.
- `--cap-drop ALL` + `--security-opt no-new-privileges:true`: the app never
  needs any Linux capability beyond what a plain non-root process gets by
  default, and never needs to escalate privileges.

Also tag the application image itself with an immutable version (a git SHA
or semver tag) for a real deployment - `compose.yaml`'s local
`fastapi-ai-task-agent:local` tag is a dev/demo convenience, not meant for
this purpose. This makes "redeploy the previous image" (see "Rollback
procedure" below) an exact, reproducible operation.

## Secret handling

- Never commit a filled-in `.env`, `.env.docker`, or a copy of
  `.env.deploy.example` - all follow the same gitignored-template
  convention.
- Never bake a real `API_KEYS`, `ANTHROPIC_API_KEY`, or `DATABASE_URL` value
  into a Docker image - pass them at container-start time (env file, secret
  manager, orchestrator-injected env vars), exactly as `compose.yaml`'s
  `env_file:` mechanism already does for local Docker use.
- Configuration errors never print secret values - see "Required environment
  variables" above.

## Logs

Container stdout/stderr logs (`app/logging_config.py`, one key=value line per
request via `app/middleware.py`'s `RequestContextMiddleware`) include:

- request ID (`X-Request-ID`, generated or safely-validated-and-echoed)
- HTTP method
- path
- response status code
- latency (`duration_ms`)
- `user_id` (only ever set post-authentication - `-` otherwise)
- safe provider-outcome metadata (e.g. which decision provider ran, whether
  it succeeded or fell back - never the underlying request/response content)

Logs **never** include: API keys, `Authorization`/`X-API-Key` header values,
database URLs, prompts, user messages, task titles/descriptions, raw model
responses, or tool argument values. `GET /ready`'s failure path follows the
same discipline more strictly than most: `app/services/readiness.py` logs
only a fixed `event=readiness_check_failed reason=<safe reason>` pair at
`WARNING` level - it deliberately never calls `logger.exception()` or sets
`exc_info=True` anywhere, and never interpolates the caught exception into
a log call at all, because a database driver's own error text (e.g. a
psycopg connection failure) can embed the DATABASE_URL, hostname, username,
or database name. `tests/test_readiness.py` proves this with a `caplog`
test using a unique sentinel string that must never appear in a log
message, its arguments, or any structured `extra` field.

## Backup before migration

Before running `python -m alembic upgrade head` against a real deployment's
database:

- **PostgreSQL**: `pg_dump` the database to a file you control.
- **SQLite**: copy the database file (e.g. `cp tasks.db tasks.db.pre-migration-backup`,
  or the container-volume equivalent) while the app is stopped or between
  requests.

This is what makes "Rollback procedure" below possible - Alembic's own
`downgrade` is intentionally blocked at this project's baseline migration
(see `alembic/versions/0001_baseline.py` and README.md), so a verified backup
is the only real safety net for a migration that turns out to be wrong.

## Rollback procedure

Because Alembic downgrade is blocked at baseline, "just downgrade" is never
an option here. Instead:

1. **Always back up first** (see above) - this is the only thing that makes
   any of the following possible.
2. To roll back a bad **application release**: redeploy the previous,
   immutably-tagged image against the *same*, unmodified database. This is
   safe exactly when the migration that shipped alongside the bad release
   was purely additive (new nullable columns/tables the old code simply
   never touches) - the common case, and the only kind of migration this
   codebase's baseline currently contains.
3. Before redeploying an older image, **check migration compatibility**:
   does the older code's models/queries reference anything the newer
   migration removed or renamed? If yes, the older image cannot safely run
   against the current schema.
4. If incompatible, the only safe path is restoring the pre-migration backup
   from step 1 onto a fresh database, then redeploying the older image
   against that restore - never a destructive Alembic downgrade.
5. After redeploying, poll `GET /ready` and confirm `200` before considering
   the rollback complete.

## Database persistence

- **PostgreSQL**: persistence is the database server's own responsibility -
  use your provider's normal backup/replication story.
- **SQLite**: the database file must live on a persistent volume, not
  container-ephemeral storage - see `compose.yaml`'s `agent_data` named
  volume for the local reference example, and the Dockerfile's `/data` mount
  point. Never store it on storage that's wiped on container
  restart/redeploy.

## Safe shutdown

The application handles `SIGTERM` gracefully: the Dockerfile's exec-form
`CMD` makes uvicorn PID 1, so the signal reaches it directly rather than
being swallowed by an intermediate shell. On shutdown, the FastAPI lifespan
disposes the database engine's connection pool and releases the readiness
check's background slot before the process exits. Both of those complete
quickly even if a readiness check happens to be stuck against a hung
database connection at the moment of shutdown - a permanently-blocked check
is bounded, daemon-thread-backed background work that is abandoned (never
joined) rather than something the shutdown path waits on. See
`app/services/readiness.py` and `tests/test_lifespan.py` for the underlying
design and its test coverage.

## Troubleshooting

- **App exits immediately on startup with a `SchemaNotAdoptedError` or
  `SchemaOutOfDateError`**: run `python -m alembic upgrade head` against the
  target database (see "Alembic migrations" above), then start the app
  again. This is the intended fail-closed behavior, not a bug.
- **App exits immediately on startup with an `ApiKeyConfigError` /
  `HardeningConfigError` / similar**: a required environment variable is
  missing or malformed - the error message names which one and why, without
  ever printing its value. Fix the environment and restart.
- **`GET /ready` returns `503` with `reason: "database_unavailable"`**: the
  database is unreachable or erroring - check connectivity/credentials
  separately (this endpoint deliberately won't tell you more than that, to
  avoid leaking connection details).
- **`GET /ready` returns `503` with `reason: "schema_out_of_date"`**: someone
  deployed application code expecting a migration that hasn't been applied
  to this database yet - run `python -m alembic upgrade head`.
- **`GET /ready` returns `503` with `reason: "check_in_progress"` or
  `"timeout"` repeatedly**: the database is likely reachable but slow, or a
  single check is stuck (e.g. a long-held lock). Investigate the database
  directly; this project's readiness check deliberately keeps at most one
  check in flight and never queues additional ones, so repeated probes alone
  can't make the situation worse.
- **Rate limiting seems inconsistent / too permissive**: confirm the app is
  actually running with `--workers 1` - the in-memory limiter's counters are
  process-local (see "Single-worker limitation" above).

## Smoke testing after a deploy

Use the extended `docker/smoke_test.ps1` (via `pwsh`, available cross-platform)
against the freshly deployed instance, with `-Full` for a real deployment
verification:

```
pwsh docker/smoke_test.ps1 -Full -BaseUrl https://your-deployment -ApiKey <a-real-key> -SmokeTestApiKey <dedicated-smoke-test-key>
```

- `-BaseUrl` and `-ApiKey` exercise the pre-existing read-only checks
  (`/health`, `/ready`, the Web UI route, unauthenticated `/tasks` → `401`,
  authenticated `/tasks` → `200`, `POST /agent/execute` in `rule_based`
  mode - no external LLM call needed).
- `-Full` is **strict/deployment mode**: it requires `-BaseUrl` and
  `-SmokeTestApiKey` to both be explicitly supplied with non-empty values,
  checked before any other check or HTTP request runs - if either is
  missing or blank, the script fails immediately with a clear error and a
  non-zero exit instead of silently skipping the authenticated lifecycle
  check. Always use `-Full` for a real post-deploy verification (the
  `deploy_smoke` CI job always passes it) - omit it only for a quick,
  intentionally partial local check of the read-only endpoints.
- `-SmokeTestApiKey` is required (no default, no fallback to a file) to run
  the authenticated task create → list → delete lifecycle check. Map it to a
  **dedicated `smoke_test_user` entry in your real deployment's `API_KEYS`**,
  never a real user's key - the app's per-`user_id` data isolation means
  this check can never see or touch any other user's tasks. The check
  creates one uniquely-titled task, confirms it's listed, then deletes
  exactly that task id in a cleanup step that runs even if an earlier
  assertion failed, and reports cleanup failure as its own distinct line if
  the delete itself doesn't succeed.

**Never run this against a shared production database using a real user's
key.** For local manual verification of the script itself, point it at a
disposable database - a temporary SQLite file created outside the repository
(the OS temp directory, never a path under the project root) or an
explicitly named, disposable PostgreSQL volume/database - never a
developer's real `tasks.db` or the normal `agent_data`/`postgres_data`
volumes, since this check creates and deletes real rows.
