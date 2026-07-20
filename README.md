# FastAPI AI Task Agent

[![CI](https://github.com/Florin980223/fastapi-ai-task-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Florin980223/fastapi-ai-task-agent/actions/workflows/ci.yml)

A small FastAPI backend for managing tasks, built as a learning project.
Tasks are persisted in SQLite via SQLAlchemy. It also has a small
rule-based agent (`/agent/...`) that can optionally use Claude to
pick which tool to run — see Configuration below.

## Setup

```bash
python -m venv venv
venv\Scripts\activate      # on Windows
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and fill in your own values. By default the
agent uses its built-in rule-based decision logic and needs no
configuration at all. `AGENT_DECISION_PROVIDER` can instead be set to:

- `anthropic` (and an `ANTHROPIC_API_KEY`) to ask Claude to pick a tool.
- `ollama` (with a local [Ollama](https://ollama.com) server running,
  `OLLAMA_BASE_URL` and `OLLAMA_MODEL`) to ask a local model to pick a tool.

Both fall back to the rule-based logic automatically if they ever fail.

Set `AGENT_MULTI_STEP_PLANNING=true` (with `AGENT_DECISION_PROVIDER=ollama`)
to let `POST /agent/execute` plan and run up to 3 existing tools for a
single request, e.g. "Create a task to buy milk and then show me all
tasks". It's off by default, only ever available through Ollama, and never
includes `delete_task` - deleting a task always goes through its normal
confirmation flow, one request at a time. Any request that can't be turned
into a safe plan runs nothing and reports a clear error instead of falling
back to guessing a single action.

`DATABASE_URL` controls where task data is persisted. It defaults to a
local SQLite file (`sqlite:///./tasks.db`) in the project root, created
automatically the first time the app starts — no setup required.

Pending clarifications, pending destructive-action confirmations, and
remembered task context ("it"/"that one") are persisted in the same
SQLite database (`ConversationState` in `app/db_models.py`), so they
survive app restarts. Each has its own TTL, in seconds:
`CONFIRMATION_TTL_SECONDS` (default `300`), `CLARIFICATION_TTL_SECONDS`
(default `900`), and `CONTEXT_TTL_SECONDS` (default `7200`) — all must
be positive integers, checked at startup. See "One worker, always"
below for the current concurrency limits of this design.

## Run

```bash
uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000/docs for the interactive API docs (Swagger UI).

## Endpoints

- `GET /health` — check that the server is running.
- `GET /tasks` — list all tasks.
- `POST /tasks` — create a task. Body: `{"title": "Buy milk", "description": "2 liters"}`
- `PATCH /tasks/{task_id}/done` — mark a task as done.
- `DELETE /tasks/{task_id}` — delete a task.

## Observability

Every `POST /agent/execute` request gets its own `run_id` (included in the
response) and a persistent trace in SQLite - what was asked, which decision
provider was configured, whether it was single- or multi-step, how it
ended (`success`, `partial`, `clarification_required`,
`confirmation_required`, `cancelled`, `no_tool`, or `error`), how long it
took, and one ordered step trace per tool that actually ran. A follow-up
reply (a clarification answer, a "yes"/"no" confirmation) is a new HTTP
request and gets its own `run_id`, linked to earlier ones only through the
shared `conversation_id`.

- `GET /agent/runs?limit=20` — the most recent runs (`limit` is capped at
  100).
- `GET /agent/runs/{run_id}` — one run's full detail, including its
  ordered step traces.

Tracing writes go through a completely separate database session/
transaction from the one handling the request's actual task operation, and
any failure there is only ever logged as a warning - it can never roll
back, invalidate, or otherwise affect a successful task operation or the
API response. No API keys, headers, or environment secrets are ever
recorded; large tool results are stored as a bounded summary rather than
in full.

## Reliability & request handling

**Request correlation.** Every response carries an `X-Request-ID`
header - either echoed back from a client-supplied one (if it's a safe
opaque token, `^[A-Za-z0-9_-]{1,100}$`) or freshly generated otherwise.
It's distinct from `run_id` above: `request_id` correlates *this HTTP
request's* logs; `run_id` is a durable, queryable record of one agent
decision. Every log line emitted during a request - including ones
already in the codebase before this feature, unchanged - is tagged with
the same `request_id`, so a single request's logs can be grepped out of
`docker compose logs` even under concurrent traffic.

**Structured logs.** One key=value line per log record, e.g.:

```
2026-07-20T10:30:00.123Z level=INFO logger=app.middleware request_id=3f9e2a msg="request completed" method=POST path=/agent/execute status=200 duration_ms=42 user_id=alice
```

`LOG_LEVEL` (default `INFO`) controls verbosity. `user_id` is only ever
attached *after* a successful authentication (never for a missing/
invalid key, and never the attempted key itself); request/response
bodies, headers (`X-API-Key`/`Authorization`), and secrets are never
logged anywhere.

**External-service timeouts & retries.**

| Provider | Timeout var (default) | Retry policy |
|---|---|---|
| Open-Meteo (weather) | `OPEN_METEO_TIMEOUT_SECONDS` (5.0) | 1 bounded retry on a transport-level failure only (connection/timeout) - never on an HTTP status error. Idempotent GET, no side effects. |
| Ollama | `OLLAMA_TIMEOUT_SECONDS` (30.0) | None - any failure already falls back to `rule_based` safely (see Configuration above); a retry would only add latency to an already-handled path. |
| Anthropic | `ANTHROPIC_TIMEOUT_SECONDS` (10.0), `ANTHROPIC_MAX_RETRIES` (2) | The SDK's own bounded retry (connection errors, 408/409/429/5xx only), made explicit/configurable instead of an invisible default. |
| Task mutations (create/update/delete/mark-done) | n/a | No HTTP calls at all - pure DB writes, nothing to retry. |

**Request size & field limits.** `MAX_REQUEST_BODY_BYTES` (default
`65536`) rejects an oversized body with `413` before routing, auth, or
parsing ever run. `ExecuteRequest.message`/`DecideToolRequest.message`
are capped at 4000 characters, and `TaskCreate`/`TaskUpdate`
title/description at 200/2000 - only an upper bound was added (no
`min_length` anywhere), so an empty agent message still behaves exactly
as before.

**Rate limiting.** A minimal in-memory, per-user, fixed-window limiter
on `POST /agent/execute` only (`RATE_LIMIT_ENABLED`, default `true`;
`RATE_LIMIT_REQUESTS`, default `120`; `RATE_LIMIT_WINDOW_SECONDS`,
default `60`) - exceeding it returns `429` with a `Retry-After` header.
It's keyed by the authenticated `user_id` only, never the raw API key,
and it is **not a security boundary** - it exists to catch accidental
abuse (e.g. a client stuck in a retry loop), not a determined attacker,
who could simply wait out the window. It is also **not correct across
multiple uvicorn workers**: each worker keeps its own independent
counter, so real throughput becomes ~N× the configured limit with N
workers - see "One worker, always" below, which this project already
runs as. Set `RATE_LIMIT_ENABLED=false` to disable it entirely.

**Error responses.** Any exception without a more specific handler
(everything else - 401, 404, 422, the conversation-state 500s - already
has one, and is unaffected) returns a fixed `{"detail": "Internal
Server Error"}` body with no exception text, traceback, or DB detail.
The real exception is logged server-side only, tagged with the
request's `request_id`.

## Evaluations

A separate, offline evaluation suite - distinct from `pytest` - measures
agent *quality* against a versioned dataset of real user messages and
expected outcomes (`evals/data/cases_v1.jsonl`, ~35 cases across all 7
categories, balanced between English and Romanian). It drives the real
`/agent/execute` endpoint (never reimplementing its routing/decision
logic), checks both response fields *and* actual database side effects,
and runs in an isolated temp-file SQLite database that's deleted
afterward - it never touches `tasks.db`.

```bash
python -m evals.run                        # rule_based mode (default)
python -m evals.run --mode mocked-ollama
python -m evals.run --mode live-ollama --allow-live-ollama
```

Three modes, each measuring something different - and the report always
says which:

| mode | what it evaluates | measures real model quality? |
|---|---|---|
| `rule_based` (default) | the real, unmodified rule-based decision logic - fully offline | no |
| `mocked-ollama` | the evaluation pipeline and the Ollama request/response contract, with a scripted stub standing in for the model - fully offline | no |
| `live-ollama` | actual quality of a real local Ollama model's decisions - requires `--allow-live-ollama` **and** `--mode live-ollama` together, plus a running local Ollama server | yes |

`live-ollama` is never invoked by `pytest`, and `get_weather` is always
mocked (in every mode, including `live-ollama`) so a score reflects the
agent's own behavior, not Open-Meteo's uptime.

Each run prints a terminal summary (overall + per-metric +
per-category accuracy, failed-case detail) and writes a JSON report
under `evals/reports/` (gitignored - a normal run never dirties `git
status`) with full reproducibility metadata: dataset version, mode,
model name, git commit, thresholds, and timestamp. Exit code `0` means
the mode's accuracy thresholds were met, `1` means they weren't, `2` is
a usage/setup error. Thresholds are mode-calibrated (`rule_based` has a
structurally lower ceiling - no Romanian tool-selection keywords and no
multi-step planning at all outside `ollama`) and overridable via
`--min-overall-accuracy`/`--min-safety-accuracy`.

## Docker

A secure, Windows-friendly Docker setup for local development and
demonstration — fully free/local, no cloud, no paid services. Requires
[Docker Desktop](https://www.docker.com/products/docker-desktop/).

### Authentication (`X-API-Key`)

Every endpoint except `GET /health` and `GET /agent/tools` requires an
`X-API-Key` header — see [Configuration](#configuration) and
`.env.docker.example` for how keys map to user ids (`API_KEYS=key:user_id,...`).
The container refuses to start if `API_KEYS` isn't configured (fails
fast, on purpose — see `app/config.py`). Each configured key acts as a
separate user: tasks, conversations, and agent-run traces created with
one key are never visible through another.

### First-time setup

```powershell
Copy-Item .env.docker.example .env.docker
# Edit .env.docker if you want real API keys instead of the placeholder
# devkey-alice / devkey-bob pair, or to enable Ollama (see below).
```

`.env.docker` is gitignored and must never be committed — it's the one
file Compose reads secrets from at runtime (`compose.yaml` has no
hardcoded keys, and none are ever baked into the image).

### Build, run, and stop

```powershell
docker compose build              # build the fastapi-ai-task-agent:local image
docker compose up -d              # start the container in the background
docker compose ps                 # check status, including health
docker compose logs -f app        # follow logs (Ctrl+C to stop watching)
docker compose down               # stop and remove the container (keeps the data volume)
docker compose build --no-cache   # rebuild from scratch after changing requirements.txt
```

The app is reachable at `http://localhost:8000` — bound to
`127.0.0.1:8000:8000` in `compose.yaml`, i.e. **localhost only**, not
your LAN. Open `http://localhost:8000/docs` for the interactive API
docs (no key required just to view them; individual "Try it out" calls
still need one).

### Checking health

```powershell
Invoke-RestMethod http://localhost:8000/health
```

Or check the container's own Docker-level health status (uses the same
`GET /health` endpoint internally, no key required):

```powershell
docker compose ps
```

### Smoke test

`docker/smoke_test.ps1` checks: `GET /health` without a key (200),
`GET /tasks` without a key (401), `GET /tasks` with a valid key (200),
and `POST /agent/execute` in `rule_based` mode (200, with a tool
selected). It never prints the API key's value, on success or failure —
only pass/fail lines and HTTP status codes.

```powershell
docker compose up -d
.\docker\smoke_test.ps1
```

Equivalent one-off checks without the script:

```powershell
Invoke-WebRequest http://localhost:8000/health -UseBasicParsing
Invoke-WebRequest http://localhost:8000/tasks -UseBasicParsing   # expect a 401 error
Invoke-RestMethod http://localhost:8000/tasks -Headers @{ "X-API-Key" = "devkey-alice" }
Invoke-RestMethod http://localhost:8000/agent/execute -Method Post -ContentType "application/json" -Headers @{ "X-API-Key" = "devkey-alice" } -Body '{"message":"Add a task to buy milk"}'
```

### Data persistence

Task and trace data lives in a named Docker volume (`agent_data`,
mounted at `/data` in the container — never the project's real
`./tasks.db`, which the container never reads, writes, or copies).
Data survives `docker compose down` and container restarts:

```powershell
docker compose down     # stops the container - the agent_data volume is KEPT
docker compose up -d    # data from before is still there
docker compose down -v  # WARNING: also deletes the agent_data volume (all tasks/traces)
```

If you'd rather inspect the SQLite file directly on the host, uncomment
the bind-mount line in `compose.yaml` and point it at a **new**
directory such as `./docker-data:/data` — never at `./tasks.db`, which
is a completely separate file used by the non-Docker `uvicorn` setup
above.

To confirm your real `tasks.db` was never touched by Docker:

```powershell
Get-FileHash .\tasks.db -Algorithm SHA256   # compare before/after any docker compose command
```

### One worker, always

The container always runs a single `uvicorn` worker (`--workers 1`),
and this isn't a performance knob to tune up yet. Pending
clarifications, pending destructive-action confirmations, and
remembered conversation context (`app/services/conversation_memory.py`)
are persisted in a `ConversationState` table in the same SQLite
database as tasks/traces — not in per-process memory — so this state
now survives app restarts and container restarts (as long as the
`agent_data` volume is kept; see "Data persistence" above).

That said:

- One worker is still the only configuration this feature has actually
  been built and tested against. Running multiple `uvicorn` workers
  against the same SQLite file introduces its own concurrency/locking
  questions (SQLite serializes writers), and the atomic, single-use
  confirmation-consumption logic in `conversation_memory.consume_confirmation`
  has only been verified under a single worker process.
- Multi-worker support is explicitly out of scope for this feature —
  don't raise `--workers` in the Dockerfile without first validating
  (and likely revisiting) that locking behavior.

### Using a local Ollama model instead of rule_based

The Docker demo defaults to `AGENT_DECISION_PROVIDER=rule_based` and
`AGENT_MULTI_STEP_PLANNING=false` — fully offline, no external
services. Ollama itself is **not** part of the compose stack; install
and run it on the host from [ollama.com](https://ollama.com), then in
`.env.docker` uncomment:

```
AGENT_DECISION_PROVIDER=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=qwen3:4b
AGENT_MULTI_STEP_PLANNING=true
```

`host.docker.internal` is how a container reaches services running on
the host machine on Docker Desktop for Windows (and Mac); `compose.yaml`
also adds an `extra_hosts` entry so the same value works on native
Linux Docker Engine. Restart the container after editing `.env.docker`:
`docker compose up -d` (Compose detects the env file change and
recreates the container).

### Security notes for this setup

- The image never contains secrets — `API_KEYS`, `ANTHROPIC_API_KEY`,
  etc. are only ever supplied at runtime via `.env.docker`
  (gitignored) or your shell environment.
- The container runs as a dedicated non-root user; only `/data` (the
  SQLite volume mount point) is writable by it.
- The published port is bound to `127.0.0.1` only — not exposed to
  your LAN by default.
- This stage intentionally has no HTTPS/TLS termination, reverse
  proxy, or production secrets manager — it's a local demo/dev setup,
  not a deployment-ready configuration. A lightweight per-user,
  single-process request-rate guard does exist (see "Reliability &
  request handling" above) — it's a basic abuse/accident guard, not a
  substitute for real perimeter security.

## Continuous Integration (CI)

GitHub Actions (`.github/workflows/ci.yml`) runs two independent jobs on
every push to `main`, every pull request targeting `main`, and on demand
via `workflow_dispatch`:

- **Test** — installs `requirements.txt`, runs the full `pytest` suite,
  then runs the offline evaluation suite in `rule_based` mode
  (`python -m evals.run --mode rule_based`). Uses fake, non-secret
  configuration only (`API_KEYS=ci-test-key-do-not-use:ci-user`,
  `AGENT_DECISION_PROVIDER=rule_based`, an isolated SQLite database under
  the runner's temp directory) — your local `tasks.db` is never touched.
- **Docker build** — builds the repo's `Dockerfile` and tags the image
  locally (`fastapi-ai-task-agent:ci`) to catch build breakage. It never
  pushes the image anywhere and needs no runtime API keys to build.

CI may download GitHub Actions, Python packages, and Docker base-image
layers from their respective registries, but the application tests and
evals themselves make no live calls to Ollama, Anthropic, or Open-Meteo —
`rule_based` mode and the test suite are fully offline and deterministic.

Run the same checks locally:

```bash
python -m pip install -r requirements.txt
python -m pip check
python -m pytest -q
python -m evals.run --mode rule_based
docker build -t fastapi-ai-task-agent:ci .
```

Results appear on GitHub under the **Actions** tab, as check marks on
each commit, and in the "Checks" section of a pull request.

## Notes

- Tasks are persisted in SQLite (see `DATABASE_URL` above) — data survives
  server restarts. Table creation happens automatically on startup; there
  are no migrations yet (schema changes require recreating the database).
