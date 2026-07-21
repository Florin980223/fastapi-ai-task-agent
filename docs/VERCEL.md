# Vercel + Neon compatibility

## Purpose and scope

This document describes what's needed to run this app as a zero-cost public
demo on **Vercel Hobby** (serverless Python functions) + **Neon PostgreSQL
Free** (a pooled connection string for the running app, a separate direct
connection string for migrations). This has since been carried out: a live
public demo now runs on this architecture - see "Verified public deployment"
below for confirmation, and the README's
[Live Demo](../README.md#live-demo) section for the URL and evaluator
walkthrough. The rest of this document, including the procedure below,
remains accurate as a description of the architecture and as reusable
reference for repeating or extending the deployment.

## Architecture

| | Local/Docker (today) | Vercel serverless |
|---|---|---|
| Process lifetime | One long-lived process, `--workers 1` | Each function instance may be a fresh cold start (fresh module state) or a reused warm instance |
| Concurrency | Single process | Many instances may run in parallel |
| Filesystem | Writable `/data` volume (SQLite) | Read-only except `/tmp` |
| Lifespan | Standard ASGI lifespan via uvicorn | Officially supported by Vercel's Python runtime; this app's exact behavior under it is not yet verified on a real deployment, and shutdown cleanup has a maximum ~500ms window |
| Database | SQLite file or Docker-local Postgres | Neon Postgres - a pooled (PgBouncer) endpoint for the app, a direct endpoint for migrations |

## Entrypoint

Current official Vercel Python support does not auto-detect `app/main.py` as
a function entrypoint, so a minimal adapter, **`app/index.py`**, re-exports
the exact same FastAPI application:

```python
from app.main import app
```

That's the entire file. It does **not** construct a second FastAPI
application - `app/main.py` itself has zero Vercel-specific code. `vercel.json`
declares the function against this adapter and routes every request to it:

```json
{
  "functions": {
    "app/index.py": {
      "excludeFiles": "{tests,evals,docker,docs,.claude,venv,.venv,.github}/**"
    }
  },
  "rewrites": [
    { "source": "/(.*)", "destination": "/app/index.py" }
  ]
}
```

No `runtime` key is set in `vercel.json` - Vercel selects the Python runtime
automatically from the `.py` file extension. The Python **language version**
is instead selected by a root **`.python-version`** file containing `3.12` -
this requests Python 3.12; Vercel chooses the supported 3.12 patch version at
build time. No exact match with the local/Dockerfile `3.12.10` patch version
is claimed - Vercel does not expose patch-level control.

`excludeFiles` (not a separate `.vercelignore`) trims the function bundle,
explicitly never excluding `app/static/`, `alembic.ini`, `alembic/`, or any
module `app/main.py`/`app/index.py` imports - `alembic/` in particular is
required at *runtime*, not just at migration time, since `GET /ready` and
startup both call `ensure_schema_is_current()`, which reads Alembic's head
revision from `alembic/versions/`.

## Lifespan, startup, and readiness

No code changes were needed for any of this - it was reviewed and confirmed
already correct, not fixed:

- `lifespan`'s `init_db()` schema verification stays exactly as-is,
  unguarded - an invalid schema still prevents the app from starting at all.
  Startup may legitimately re-run independently on every cold-started
  instance; this is expected, not a bug.
- `app/services/readiness.py`'s per-process single-in-flight-check design is
  exactly the right granularity for "one instance, one in-flight check" - no
  cross-instance coordination is needed, since each instance only proves its
  own DB connectivity.
- `GET /health` stays a trivial, DB-free liveness check, unchanged.
  `GET /ready` already performs a real DB+Alembic check on every invocation
  with no caching, and already redacts every failure to a fixed safe reason
  string.
- Correctness never depends on the ~500ms shutdown window completing - every
  state-changing operation (tasks, conversation state, confirmations, agent
  tracing) already commits synchronously within the request that performs
  it. `engine.dispose()`/`readiness.shutdown()` are best-effort cleanup only.

## SQLAlchemy and Neon pooling strategy

A new, **fully opt-in** `DB_POOL_MODE` environment variable controls the
engine `app/database.py` builds - default behavior (`DB_POOL_MODE` unset, or
`"default"`) is byte-for-byte unchanged for SQLite, local Docker Postgres,
and the local demo stack.

Set `DB_POOL_MODE=serverless` only for a Vercel + Neon deployment:

- **`NullPool`** - no local connection pool. Neon's pooled endpoint already
  provides pooling at the infrastructure level; stacking SQLAlchemy's own
  pool on top, multiplied across many concurrent, often single-invocation
  serverless instances, works against that model.
- **`prepare_threshold=None`** - disables psycopg3's automatic server-side
  prepared-statement caching, required when connecting through a PgBouncer
  **transaction-pooling** endpoint (a prepared statement tied to one
  physical backend connection is unsafe there).
- **`connect_timeout=5`** - bounds how long a connection *attempt* may hang
  against a sleeping/unreachable Neon compute, for real business requests
  too, not just `/ready`.
- **SSL enforcement**: a non-local PostgreSQL URL used in serverless mode
  must include `sslmode=require` in its query string, or the engine refuses
  to build (`InsecureRemoteDatabaseUrlError`, a safe, fixed message - never
  the URL or password). `localhost`/`127.0.0.1` may omit it, which is what
  keeps local verification usable without a locally-configured certificate.
  Neon's own connection strings already include `?sslmode=require` by
  default, so this should never fire against a correctly-copied real URL.

Setting `DB_POOL_MODE=serverless` and a `sqlite://` `DATABASE_URL` together
is refused at config-load time - a read-only-except-`/tmp` filesystem cannot
safely persist SQLite data.

## Runtime URL vs. migration URL

Two distinctly-named, explicit environment variables:

- **`DATABASE_URL`** - the Neon **pooled** connection string. Read by the
  running app exactly as today. This is the *only* database URL ever
  present in the Vercel runtime's configured environment variables.
- **`MIGRATION_DATABASE_URL`** - the Neon **direct** connection string, for
  Alembic/admin use only. Never read anywhere under `app/` (verified by a
  dedicated test); never set in the Vercel runtime's own environment.

A new `MIGRATION_MODE` environment variable, read only by
`alembic/env.py`:

- Unset (every local/Docker/CI Alembic invocation today) → falls back to
  `DATABASE_URL`, byte-for-byte identical to before this feature existed.
- `"production"` → **requires** `MIGRATION_DATABASE_URL` to be set; if
  absent, raises immediately (`MigrationUrlRequiredError`) - Alembic never
  silently falls back to the pooled `DATABASE_URL` for a production
  migration. As defense in depth (not the primary mechanism - the primary
  mechanism is requiring the distinctly-named variable at all), the
  supplied URL is also checked: it must not look like a pooled Neon
  endpoint (hostname containing `-pooler`, `PooledUrlForMigrationError`
  otherwise), and it must include `sslmode=require` if non-local
  (`InsecureRemoteDatabaseUrlError` otherwise).

Run a real production migration as:

```
MIGRATION_MODE=production MIGRATION_DATABASE_URL=<the direct URL> python -m alembic upgrade head
python -m alembic check
```

Never by temporarily overwriting `DATABASE_URL`.

## Static assets

FastAPI's existing `StaticFiles` mount is kept exactly as-is - no Vercel
`public/` directory, no asset duplication. The catch-all rewrite sends every
request through the one function, so the Web UI continues to work
unchanged. The static directory is tiny (~40KB, 4 files) - moving it to
Vercel's `public/` convention was considered and rejected: no real benefit
at this size, and it would split the UI's single source of truth.

## Environment variables

See **`.env.vercel.example`** for the full placeholder template. Summary:

**Runtime (Vercel Production environment variables only):**
`DATABASE_URL` (pooled), `API_KEYS` (one dedicated disposable demo key),
`AGENT_DECISION_PROVIDER=rule_based`, `AGENT_MULTI_STEP_PLANNING=false`,
`DB_POOL_MODE=serverless`, `READY_CHECK_TIMEOUT_SECONDS`, `RATE_LIMIT_*`.

**Migration-only (never set in the Vercel runtime):**
`MIGRATION_DATABASE_URL` (direct), `MIGRATION_MODE=production` - set only
for the duration of a real migration command, in whoever's local shell runs
it.

**Preview deployments**: configure `DATABASE_URL`/`API_KEYS` **only** in
Vercel's Production environment scope, leaving Preview/Development empty.
A Preview deployment then fails closed at import time (the existing
`ApiKeyConfigError`) rather than risking silently sharing or mutating the
production demo's Neon data - simpler and free, unlike a per-preview
disposable Neon branch database (which exists as an option but is
deliberately not set up for this feature).

## API-key strategy

Unchanged mechanism (`app/services/auth.py`'s fail-closed, constant-time
comparison). Generate one disposable demo key exactly as
[docs/LOCAL_DEMO.md](LOCAL_DEMO.md) already documents, and deliver it to an
evaluator out-of-band (a separate message) - never embedded in HTML/JS.
This is already guaranteed structurally: the Web UI stores the key only in
`sessionStorage`, never hard-coded, and a compatibility test asserts the
configured key never appears as a literal byte sequence anywhere under
`app/static/`.

## Rate-limit limitation (documented honestly)

`app/services/rate_limiter.py`'s in-memory per-process counter provides
**no cross-instance guarantee whatsoever** under Vercel - every
concurrently-scaled instance keeps its own independent counter, and every
cold start resets it to zero. It stays enabled by default (still
meaningfully slows abuse *within* one warm instance) but must never be
described as global or production-grade rate limiting. The real security
boundary for the public demo remains API key authentication, which is
unaffected by this limitation.

## Local serverless-compatibility verification (performed during implementation)

Reusing the existing `docker/demo_start.ps1`/`docker/demo_cleanup.ps1`
stack (no new script, no new secrets file) - a real local Postgres was
used to exercise `DB_POOL_MODE=serverless` end-to-end:

- `/health` → 200, `/ready` → 200 `{"status":"ready"}`, unauthenticated
  `/tasks` → 401.
- Full task CRUD, agent clarification flow, and destructive-confirmation
  flow (trigger → confirm "yes" → verify 404 afterward) all worked
  correctly against a real Postgres under `NullPool` + `prepare_threshold=None`.
- `MIGRATION_MODE=production` + `MIGRATION_DATABASE_URL` exercised for real
  (`alembic check`) against the same local Postgres, with `DATABASE_URL`
  deliberately pointed elsewhere - confirmed Alembic actually used the
  explicit migration URL, not a fallback.

**A real finding worth documenting for anyone repeating this verification**:
when constructing a local `DATABASE_URL`/`MIGRATION_DATABASE_URL` for this
kind of test, use `127.0.0.1`, not `localhost`. On at least one
Windows + Docker Desktop setup, psycopg3's connection establishment via
`localhost` exhibited a multi-second IPv6-then-IPv4 fallback delay
consistent with `connect_timeout` on every fresh connection (expensive
under `NullPool`, which opens a new connection per checkout) - `127.0.0.1`
avoided it entirely (sub-100ms). This is a local-verification-methodology
detail, not an application bug and not expected to affect a real Neon
deployment (which uses ordinary internet routing to a real hostname, not a
Windows/Docker Desktop loopback port-forward).

## Verified public deployment

The public demo described in the README's [Live Demo](../README.md#live-demo)
section has been deployed and externally verified, from both desktop and
mobile clients:

- Deployment to Vercel succeeded.
- `GET /health` returned `200`.
- `GET /ready` returned `200`.
- The Web UI loaded and worked correctly.
- Authenticated task CRUD worked correctly.
- The agent clarification flow worked correctly.
- The destructive-action confirmation flow worked correctly.
- Run history persisted correctly across requests.
- External verification from a mobile client succeeded, in addition to
  desktop.

No credentials, keys, or connection strings are recorded here — see
"API-key strategy" above for how the demo key is distributed, and
"Environment variables" above for what's configured where.

## Neon creation and migration procedure (reference; already performed for the live demo)

1. Create a free Neon project/database via Neon's dashboard.
2. Copy both connection strings Neon provides: the **pooled** one (hostname
   contains `-pooler`) and the **direct** one - both already include
   `?sslmode=require` by default; confirm this before proceeding.
3. Locally: `MIGRATION_MODE=production`, `MIGRATION_DATABASE_URL=<the direct URL>`,
   then `python -m alembic upgrade head` and `python -m alembic check` - a
   deliberate, human-run command, never automated by CI or a deploy step.
4. Configure Vercel's **Production** environment variables only (see
   "Environment variables" above). `MIGRATION_DATABASE_URL`/`MIGRATION_MODE`
   are never set here.
5. Deploy.
6. Verify `/health`, `/ready`, the Web UI, and that protected routes still
   return 401 without a key - against the real deployment. (For the live
   demo, see "Verified public deployment" above for the result.)
7. Retiring the demo later: rotate/delete the demo API key, and optionally
   suspend/delete the Neon project and Vercel deployment.

## Rollback and data safety

- Never upload `tasks.db` or any personal data to Neon - the Neon database
  starts empty; only fictional demo data is ever created in it, under a
  dedicated demo user (the same convention as
  [docs/LOCAL_DEMO.md](LOCAL_DEMO.md)).
- Always back up the Neon database (`pg_dump` against the direct URL)
  before running `alembic upgrade head` against it.
- No Alembic downgrade - the project's baseline migration intentionally
  blocks it, exactly as documented in
  [docs/DEPLOYMENT.md](DEPLOYMENT.md)'s "Rollback procedure".
- When retiring the demo, remove/rotate the demo API key and, if desired,
  suspend or delete the Neon project and Vercel deployment.

## Zero-cost safeguards

- Every file this feature adds or modifies is inert configuration/code/
  docs/tests - none of them create a Vercel project, a Neon project, a
  deployment, or any billing relationship by existing in the repository.
- `.env.vercel.example` contains only placeholders, never a real credential.
- No file references a Vercel Pro trial, marketplace add-on, AI Gateway,
  paid database tier, analytics add-on, or custom domain anywhere.
- Local verification used only the already-existing free local Postgres
  (the `compose.demo.yaml` stack) - no new paid or cloud dependency was
  introduced to verify this feature either.

## Limitations

- This app's exact behavior under Vercel's (officially supported) lifespan
  handling is not yet verified on a real deployment.
- `prepare_threshold=None` for PgBouncer compatibility is a well-documented
  psycopg3 pattern, validated locally against a real Postgres - not yet
  validated against a real Neon pooled endpoint, since none exists yet.
- Rate limiting remains per-instance/best-effort only under Vercel.
- Cold-start latency (both Vercel's own and Neon's scale-to-zero wake time)
  may make the first request after idle noticeably slower.
- No new CI job - the new compatibility tests run in the existing `test`
  job; nothing in CI ever touches Vercel or Neon.
- `.python-version` requests Python 3.12; Vercel chooses the supported 3.12
  patch version. No exact match with the local `3.12.10` patch version is
  claimed.
