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

Both make exactly one constrained repair attempt if the model's first
response is malformed or fails validation, then — only if the original
request is single-step, non-destructive, unambiguous, and has no bare
contextual reference ("it"/"that one") — fall back to the rule-based
provider. Anything else (a multi-step-shaped request, destructive intent,
a contextual reference, or a failure caused specifically by bad
arguments) fails cleanly and asks you to rephrase, instead of silently
guessing via the deterministic fallback. See "Provider architecture and
the safe decision flow" under Evaluations below for the full policy and
why it's this conservative.

Set `AGENT_MULTI_STEP_PLANNING=true` (with `AGENT_DECISION_PROVIDER=ollama`)
to let `POST /agent/execute` plan and run up to `AGENT_MAX_PLAN_STEPS`
(default `3`, minimum `2`) existing tools for a single request, e.g.
"Create a task to buy milk and then show me all tasks". It's off by
default, only ever available through Ollama, and never includes
`delete_task` - deleting a task always goes through its normal
confirmation flow, one request at a time. Any request that can't be turned
into a safe plan runs nothing and reports a clear error instead of falling
back to guessing a single action.

`DATABASE_URL` controls where task data is persisted. It defaults to a
local SQLite file (`sqlite:///./tasks.db`) in the project root. Schema
is managed by Alembic, not automatically on startup — see "Database
migrations (Alembic)" below, which a fresh clone needs to run **once**
before the app will start at all.

Pending clarifications, pending destructive-action confirmations, and
remembered task context ("it"/"that one") are persisted in the same
SQLite database (`ConversationState` in `app/db_models.py`), so they
survive app restarts. Each has its own TTL, in seconds:
`CONFIRMATION_TTL_SECONDS` (default `300`), `CLARIFICATION_TTL_SECONDS`
(default `900`), and `CONTEXT_TTL_SECONDS` (default `7200`) — all must
be positive integers, checked at startup. See "One worker, always"
below for the current concurrency limits of this design.

## Database migrations (Alembic)

Schema is versioned with [Alembic](https://alembic.sqlalchemy.org/)
(`alembic.ini`, `alembic/env.py`, `alembic/versions/`) instead of being
created automatically. **Alembic — a human running `python -m alembic
upgrade head` — is the only thing that ever creates, alters, or stamps
a real application database's schema.** On every startup, the app
(`app.database.init_db()`, via `app/services/schema_migration.py`)
only ever *verifies* that the database is already fully migrated —
never creates, alters, migrates, or stamps anything itself — and fails
fast with a clear, actionable error otherwise:

- Empty database → refuses to start; run `python -m alembic upgrade
  head` first.
- Has tables but was never adopted by Alembic (no `alembic_version`
  table) → refuses to start; run the adoption procedure below first.
- Adopted, but not at the latest revision → refuses to start; run
  `python -m alembic upgrade head`.
- Adopted and current → starts normally (fast, silent, same "safe to
  call on every startup" promise as before).

`Base.metadata.create_all()` still exists in the codebase, but only for
isolated pytest databases, eval temporary databases, and the dedicated
test/eval fixtures that bootstrap their own throwaway engines
(`tests/conftest.py`'s `restart_client_factory`,
`evals/isolation.py`'s `isolated_app_client()`) — it is never reachable
from a normal dev/Docker/production startup anymore.

### Fresh database (new clone, or `tasks.db` deleted)

```powershell
python -m alembic upgrade head
uvicorn app.main:app --reload
```

### An existing or legacy `tasks.db`: the `check` / `adopt-legacy` CLI

`app/services/schema_migration.py` is also a small CLI
(`python -m app.services.schema_migration --help`) for the two
non-fresh cases below. Every command requires an explicit
`--database-path` (and, for adoption, an explicit `--backup-path`) —
it never reads `DATABASE_URL` and never defaults to `tasks.db`, so
there's no way to accidentally target the wrong file.

**An existing `tasks.db` that already has `user_id` and `conversation_states`** —
verify first, every time, never blindly `stamp`:

```powershell
# 1. Backup.
Copy-Item tasks.db "tasks.db.backup-$(Get-Date -Format yyyyMMddHHmmss)"

# 2. Verify - read-only, writes nothing, exits 0 only if the schema
#    already matches the baseline exactly.
python -m app.services.schema_migration check --database-path tasks.db

# 3. Adopt - no DDL, no data touched.
python -m alembic stamp head
```

**A legacy, pre-authentication `tasks.db`** (missing `user_id`,
possibly missing `conversation_states` - `check` above would report a
non-empty diff): `adopt-legacy` performs the full adoption in one
command - it refuses to run without a backup that's byte-identical
(SHA-256 verified) to the database it's about to transform, adds the
missing `user_id` columns (backfilled with the `__unmigrated__`
sentinel - see below) and their indexes, creates `conversation_states`
with its own indexes/unique constraint, re-verifies every pre-existing
row's pre-existing values are unchanged and the resulting schema
exactly matches the baseline, and only stamps `0001_baseline` if both
of those checks pass - it never runs `alembic upgrade head` against an
existing database.

```powershell
# 1. Backup - adopt-legacy refuses to run without this, and refuses
#    unless its SHA-256 matches tasks.db exactly.
Copy-Item tasks.db "tasks.db.backup-$(Get-Date -Format yyyyMMddHHmmss)"

# 2. Adopt.
python -m app.services.schema_migration adopt-legacy `
    --database-path tasks.db `
    --backup-path "tasks.db.backup-<the timestamp from step 1>"

# 3. Confirm.
python -m app.services.schema_migration check --database-path tasks.db
```

If `adopt-legacy` refuses for any reason (missing/mismatched backup,
data that doesn't look unchanged after the transformation, or a
schema that still doesn't match afterward), it prints exactly what it
found, stamps nothing, and leaves the database in whatever state it
was in — restore from the backup if you want to abandon the attempt
rather than diagnose and re-run (each step it performs is itself
idempotent, so simply re-running with a *fresh* backup of the current
state is also safe).

Rows created before authentication existed are preserved with
`user_id = "__unmigrated__"` (a reserved sentinel that can never match
a real configured user, so they become inert/inaccessible via the API
rather than silently landing on whichever user authenticates first —
see `.env.example` for how to manually reclaim them).

`app/services/db_migrate.py`'s `backfill_legacy_user_id_columns` is
**not** converted into an Alembic migration and is **not** retired by
this change — `adopt-legacy` calls it directly, unmodified, as one step
of its transformation. Turning it into a migration would either
duplicate this exact logic in two places, or force every not-yet-adopted
database through Alembic before it can even reach a state Alembic
recognizes, which is circular. It stays in the codebase for anyone who
still has a never-yet-adopted pre-auth database; retiring it is a
separate, later decision once none are believed to exist anymore.

### Writing a new migration

```bash
alembic revision -m "describe the change"    # or --autogenerate, then review carefully
alembic upgrade head
```

`env.py` configures `render_as_batch=True`, since SQLite can't perform
most `ALTER TABLE` operations directly — this makes
`op.batch_alter_table(...)` available and correct for any future
migration without extra setup. **Never trust `--autogenerate` output
blindly** — diff it against `app/db_models.py` by hand before
committing, the same way the baseline revision
(`alembic/versions/0001_baseline.py`) was reviewed.

The baseline's `downgrade()` deliberately raises rather than dropping
every table: there's no revision below it, so "downgrading" could only
ever mean destroying all data. The rollback path for the baseline is
restoring a pre-migration backup, not `alembic downgrade`. Future
migrations should implement a real `downgrade()` unless they're
similarly irreversible, in which case block-and-document the same way.

## Run

```bash
uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000/docs for the interactive API docs (Swagger UI),
or http://127.0.0.1:8000/ for the browser demo UI — see "Web UI" below.

## Endpoints

- `GET /health` — liveness only: is the process alive and responsive. Never touches the database.
- `GET /ready` — readiness: database connectivity, Alembic schema revision, and required configuration, within a small bounded timeout. `200` when ready, `503` otherwise. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)'s "Health vs. readiness" section for the precise semantics.
- `GET /tasks` — list all tasks.
- `POST /tasks` — create a task. Body: `{"title": "Buy milk", "description": "2 liters"}`
- `PATCH /tasks/{task_id}/done` — mark a task as done.
- `DELETE /tasks/{task_id}` — delete a task.

## Web UI

A small, dependency-free browser UI for demoing the task list and the agent
without needing `curl`/Swagger — plain HTML, CSS, and vanilla JavaScript
(ES modules, no framework, no build step, no CDN), served directly by this
same FastAPI app from `app/static/`. It doesn't add, hide, or reimplement
any behavior: every action calls one of the existing endpoints listed
above and in "Endpoints"/"Observability", and every rule (auth, per-user
isolation, clarification/confirmation flows) is enforced by the backend
exactly as it already was — the UI only ever reflects what a response
says.

**Starting it:**

```bash
uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000/** (or, via Docker, **http://localhost:8000/**
after `docker compose up -d` — see "Docker" below). `/docs`, `/redoc`, and
every existing endpoint keep working exactly as before; the UI is mounted
last and only ever serves a path nothing else claims.

**Entering your API key:** on first load you'll see an "Enter your API key"
screen — paste one of your configured `API_KEYS` values (e.g. one of the
`devkey-alice`/`devkey-bob` pair from `.env.example`/`.env.docker.example`).
The key is sent as the normal `X-API-Key` header on every request and is
stored **only** in this browser tab's `sessionStorage` — never in
`localStorage`, never written to disk, never sent anywhere but this server.
A "Clear API key" button in the header removes it and returns to the entry
screen at any time; it also disappears automatically when you close the tab.

**Local-demo security limitations** (by design, for a local/portfolio demo
— not a production multi-tenant deployment):

- `sessionStorage` is readable by any script running on the page. The
  strict `Content-Security-Policy` this UI is served under (no
  `'unsafe-inline'`, no `'unsafe-eval'`, no external/CDN source — see
  "Security notes for this setup") is the mitigation, but this remains a
  local-demo pattern, not a hardened production auth design.
- No CSRF exposure is introduced by this UI: authentication is a custom
  header sent by JavaScript, never an ambient cookie, so classic CSRF
  doesn't apply — but a successful XSS would still be able to read the
  stored key for as long as the tab stays open.
- `/docs`/`/redoc` still load Swagger UI's/ReDoc's own JS/CSS from a CDN —
  unrelated pre-existing FastAPI behavior, explicitly out of scope here,
  and the only exemption from this UI's own strict CSP.
- Same single-worker/in-memory rate-limiter constraints as always — see
  "One worker, always" below; this UI doesn't change them.

**Demo workflow:** add a task, mark it done, then switch to the Agent tab
and try something ambiguous like "create a task" — answer its clarifying
question, then try "delete task 1" and confirm it when asked. Switch to
Run History to see both requests recorded with their status, selected tool,
and step trace.

**Screenshots:** _(placeholder — add a screenshot of each of the three
tabs — Tasks, Agent, Run History — here once available)_

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
expected outcomes (`evals/data/cases_v1.jsonl`, 43 cases across 9
categories, mostly balanced between English and Romanian). It drives the
real `/agent/execute` endpoint (never reimplementing its routing/decision
logic), checks both response fields *and* actual database side effects,
and runs in an isolated temp-file SQLite database that's deleted
afterward - it never touches `tasks.db`.

The 9 categories: `single_step_tool_selection`, `argument_extraction`,
`clarification_behavior`, `destructive_confirmation`, `no_tool_messages`,
`safe_multi_step_planning`, `destructive_multi_step_rejection`,
`context_usage` (does `resolve_remembered_task_id` correctly fill a
missing task id from "it"/"that one", including across a
create-then-confirm-then-delete sequence?), and
`malformed_output_recovery` (does a scripted broken-then-repaired, or
broken-twice, Ollama response get handled safely - repaired and executed,
or safely refused - by the shared validation/repair layer? See "Provider
architecture and the safe decision flow" below). The last category is
only ever meaningfully exercised in `mocked-ollama` mode - `rule_based`
never touches an Ollama seam, and a real `live-ollama` model can't be
scripted, so both modes just run the message through their own real
logic instead and land on the same answer.

```bash
python -m evals.run                        # rule_based mode (default)
python -m evals.run --mode mocked-ollama
python -m evals.run --mode live-ollama --allow-live-ollama
```

The `pytest` suite's own mocked-provider tests (no `--mode` flag - these
are ordinary `pytest` tests, included in the default `python -m pytest`
run and in CI) cover the providers and the shared validation/repair layer
directly, including malformed-JSON/unknown-tool/wrong-type/unknown-
argument/timeout/connection-failure cases and repair-retry success and
failure:

```bash
python -m pytest tests/test_decision_validation.py tests/test_ollama_decision_provider.py tests/test_anthropic_decision_provider.py tests/test_agent_decision.py tests/test_multi_step_planning.py -v
```

None of these ever make a real Ollama or Anthropic call - every one mocks
the provider's own private HTTP-call function or client factory (see
"Provider architecture and the safe decision flow" below).

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

**Acceptance thresholds, by category of test:**

| what | gate | current threshold |
|---|---|---|
| Deterministic `rule_based` evals | `python -m evals.run` (CI) | overall ≥ 0.55, safety ≥ 0.6 |
| Mocked provider `pytest` tests | `python -m pytest` (CI) | 100% pass - binary, no percentage applies |
| `mocked-ollama` evals | `--mode mocked-ollama` (CI) | overall ≥ 0.95, safety ≥ 1.0, **and** `destructive_confirmation`/`malformed_output_recovery` categories = 1.0 each |
| Optional `live-ollama` evals | `--mode live-ollama --allow-live-ollama` (local only, never CI) | overall ≥ 0.75, safety ≥ 1.0 - informational |

The per-category floors on `mocked-ollama` exist so a systemic failure
concentrated in one category can't hide behind a passing overall average
(`evals/runner.py::_determine_exit_code`) - per-category accuracy is
always printed regardless, but these two specifically also gate the exit
code, since "mostly right" isn't acceptable for either a destructive
action's confirmation gating or the malformed-output validation/repair/
fallback pipeline staying safe.

### Provider architecture and the safe decision flow

Three decision providers share one contract (`ToolDecision`/`AgentPlan` in
`app/services/tool_decision.py`/`agent_plan.py`) and one validator
(`app/services/tool_schemas.py::validate_tool_call` - checks the tool
exists, arguments are a dict, every argument key is one the tool actually
accepts, and any present value has the right type):

- **`rule_based`** - ordered keyword rules + regex argument extraction, no
  network call, no LLM. The deterministic baseline; never wrapped,
  retried, or gated by anything below.
- **`anthropic`** - native Claude tool-use. One call; on a malformed/
  invalid response, one constrained repair attempt (resends the failed
  tool-use plus a `tool_result` marked as an error, asking for a
  correction); still invalid → falls through to the shared fallback-safety
  gate below.
- **`ollama`** - OpenAI-style tool-calling over `POST /api/chat` for
  single-step decisions; genuine JSON-Schema-constrained structured output
  (Ollama's `format` parameter, set to the plan's own schema) for
  multi-step planning. Same one-repair-attempt shape as Anthropic.
  Zero app-level retries beyond that one attempt for either call type.

**The fallback-safety gate** (`app/services/agent_decision.py::_safe_to_fall_back`,
only reachable after a configured Anthropic/Ollama provider has already
failed once and, if repairable, failed its one repair attempt too) decides
whether falling back to `rule_based` is safe for *this specific request*:
refuses (raises `UnsafeFallbackError`, which the API turns into a clean
"please rephrase" response - never executes anything) when the message is
multi-step-shaped, contains destructive intent, contains a bare
contextual reference ("it"/"that one"), the failure was specifically bad
arguments on an otherwise-recognized tool, or the message matches more
than one of `rule_based`'s own keyword rules (ambiguous by its own
reckoning). Falls back only when none of those apply - in that narrow
case `rule_based`'s extraction never invents a value, so the user's intent
is preserved rather than guessed at.

A **pre-model guard** (`agent_planner._has_destructive_intent`) blocks
multi-step planning outright for a destructive-sounding clause *before*
ever asking the model - a small model asked to plan "...and then delete
it" has been observed live silently substituting `mark_task_done` instead
of refusing. This guard (and the fallback-safety gate above) can only ever
*block* - neither ever rewrites a selected tool, invents an argument,
executes anything directly, or bypasses the normal confirmation flow.

**Latency budget:** worst case for one decision call is bounded by
`2 × OLLAMA_TIMEOUT_SECONDS` (or `2 × ANTHROPIC_TIMEOUT_SECONDS`) - the
first call's own timeout, plus, only on a repairable failure, one repair
call under the same timeout. A network/timeout failure never triggers a
repair attempt (it goes straight to the fallback-safety gate), so it never
pays that cost.

**Observability without a schema change:** structured log lines (never
raw prompts, model responses, message text, or argument values - only
fixed fields: provider, model, latency, whether a repair happened, a
fixed-vocabulary failure category, and outcome) are emitted at each
provider call and at multi-step planning time, closing the gap between
what a trace's `AgentRun`/`AgentRunStep` rows show (only steps actually
*attempted*) and what was actually *planned*. `AgentRun.decision_provider`
remains the only piece of this stored in the database - everything else
is log-only, on purpose (no migration for this).

### Limitations of small local models

A small local model (this project has been run against `qwen3:4b`) can be
*confidently wrong* without ever producing a malformed response at all -
no validation layer or repair attempt catches that, only the pre-model
destructive-intent guard (for the one failure mode it's specifically built
for) and your own judgment reading a `live-ollama` report. The
`malformed_output_recovery` eval category measures whether the *pipeline*
stays safe when a response is broken - it says nothing about how *often*
a real model actually produces one; that frequency is exactly what an
occasional local `live-ollama` run is for, informationally, never as a CI
gate.

### Interpreting eval scores

- **`rule_based`**'s ceiling is structurally below 100% by design (no
  Romanian keywords, no multi-step planning outside `ollama`) - its
  threshold (0.55/0.6) reflects that honest ceiling, not a quality bar.
- **`mocked-ollama`** at anything less than ~100% usually means a real
  pipeline/contract bug (parsing, validation, safety gating, or tracing),
  since the "model" only ever echoes back the case's own expected answer
  or a deliberately scripted malformed response - there's no actual
  judgment being measured.
- **`live-ollama`** is the only mode that measures real model judgment.
  Read its per-category breakdown, not just the overall number - a low
  `safe_multi_step_planning` score with a perfect `destructive_confirmation`/
  `destructive_multi_step_rejection` score means the model is imprecise
  but not unsafe; the reverse would be the one result worth stopping and
  investigating immediately.

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

### Database migrations

The container never runs a migration automatically on startup — the
same "verify, don't mutate" startup check described in "Database
migrations (Alembic)" above applies inside Docker too, against
whatever database is in the `agent_data` volume. This means **a brand
new volume needs one explicit migration command before the app will
start successfully** — `docker compose up -d` alone will crash-loop
against an empty `/data` otherwise. `alembic.ini` and `alembic/` are
copied into the image specifically so these commands work.

```powershell
# 1. Backup first - works whether or not the container is currently
#    running, since it reads the volume directly.
docker run --rm -v fastapi-ai-task-agent_agent_data:/data -v ${PWD}:/backup alpine `
    cp /data/tasks.db /backup/tasks.db.docker-backup-$(Get-Date -Format yyyyMMddHHmmss)

# 2. Check the current revision (read-only; safe even against an
#    empty/never-migrated volume).
docker compose run --rm app python -m alembic current

# 3. Apply migrations. Always `run --rm` (a one-off container against
#    the same volume), not `exec` - `exec` requires an already-running,
#    already-healthy container, which a fresh or out-of-date volume
#    will never reach.
docker compose run --rm app python -m alembic upgrade head

# 4. Start the app normally - the startup check now passes.
docker compose up -d
```

Use the same three steps (backup, `alembic current`, `alembic upgrade
head`) before restarting after any image update that includes a new
migration - never let `docker compose up -d` be the first command run
against an out-of-date volume.

### Build, run, and stop

```powershell
docker compose build              # build the fastapi-ai-task-agent:local image
docker compose up -d              # start the container in the background (after migrating - see above)
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

### PostgreSQL (optional)

SQLite (the default above) needs no setup and is fine for local use.
`compose.yaml` also ships a profile-gated `postgres` service — a fully
local, free, production-style alternative — for anyone who'd rather run
against a real network database. It never starts on a bare
`docker compose up`; you opt in explicitly with `--profile postgres`.

```powershell
# 1. Edit .env.docker: uncomment the POSTGRES_USER/PASSWORD/DB lines and
#    the postgresql+psycopg:// DATABASE_URL line, and comment out the
#    sqlite:// DATABASE_URL line - see .env.docker.example.

# 2. Start Postgres and wait for it to report healthy.
docker compose --profile postgres up -d --wait postgres

# 3. Migrate the (empty, first time) Postgres database - same
#    "verify, don't mutate on startup" rule as SQLite above applies here
#    too, so this is required before the app will start against it.
docker compose --profile postgres run --rm app python -m alembic upgrade head

# 4. Now start the app itself, against the same profile.
docker compose --profile postgres up -d
```

There's deliberately no `depends_on: postgres` on the `app` service — Compose
implicitly activates a profile-gated service's profile whenever an
always-on service depends on it, which would silently start `postgres` on
every bare `docker compose up` and defeat the point of gating it behind a
profile. That's why the steps above are explicit and ordered instead.

Inspect the data directly with `psql` inside the container:

```powershell
docker compose --profile postgres exec postgres psql -U taskagent -d taskagent
```

Postgres is also published to the host at `127.0.0.1:55432` (never
`5432`, to avoid colliding with a Postgres already running natively on
the host) — only for a host-side `psql`/GUI client or opt-in local
integration tests; the `app` container itself always talks to it over the
Compose-internal network via the service name `postgres`, not through
this port.

Stop it the same way as `app`:

```powershell
docker compose --profile postgres down    # stops and removes the container, keeps postgres_data
```

To remove the Postgres data too, remove *only* its own named volume —
**never** `docker compose down -v` or `docker compose --profile postgres
down -v`, both of which delete every named volume in `compose.yaml`
(including `agent_data`), regardless of profiles:

```powershell
docker volume rm fastapi-ai-task-agent-postgres-data
```

To switch back to SQLite, restore `.env.docker`'s `DATABASE_URL` to
`sqlite:////data/tasks.db` and comment the Postgres block back out, then
restart with a bare `docker compose up -d` (no `--profile`).

An opt-in integration test (`tests/test_postgres_integration.py`, skipped
unless `POSTGRES_TEST_DATABASE_URL` is set) exercises the same Alembic
migration and basic CRUD against a dedicated local `taskagent_test`
database — never the real `taskagent` database or `tasks.db`:

```powershell
docker compose --profile postgres exec postgres psql -U taskagent -d taskagent -c "CREATE DATABASE taskagent_test OWNER taskagent;"
$env:POSTGRES_TEST_DATABASE_URL = "postgresql+psycopg://taskagent:<password>@localhost:55432/taskagent_test"
python -m pytest tests/test_postgres_integration.py -v
```

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
  proxy, or production secrets manager — it's a local demo/dev setup.
  See [Deployment](#deployment) below for what's needed to run this as
  a real deployment. A lightweight per-user, single-process
  request-rate guard does exist (see "Reliability & request handling"
  above) — it's a basic abuse/accident guard, not a substitute for real
  perimeter security.
- Every response (API and the Web UI alike) carries `X-Content-Type-Options:
  nosniff`, `Referrer-Policy: no-referrer`, and `X-Frame-Options: DENY`
  (`app/middleware.py`'s `SecurityHeadersMiddleware`). A strict
  `Content-Security-Policy` (`default-src 'self'` and friends — no
  `'unsafe-inline'`, no `'unsafe-eval'`, no external/CDN source) is also
  added everywhere **except** `/docs`, `/redoc`, and `/openapi.json`,
  which are exempted only because Swagger UI/ReDoc's own default HTML
  loads its JS/CSS from a CDN — those three keep working exactly as
  before.

## Deployment

The setup above (SQLite, single `.env`/`.env.docker`, `docker compose up`) is
a local dev/demo workflow. For a real, public-facing deployment — required
environment variables, PostgreSQL setup, the explicit `alembic upgrade head`
step, health vs. readiness (`GET /health` vs. the new `GET /ready`), the
single-worker limitation, secret handling, logs, backup-before-migration,
rollback, database persistence, safe shutdown, troubleshooting, and
post-deploy smoke testing — see **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

This project stays platform-neutral: the runbook does not assume any
specific cloud provider, container orchestrator, Redis, Celery, or paid
service — those remain deliberately out of scope.

## Local production-like demo

A separate, isolated, zero-cost Compose stack (`compose.demo.yaml`) that
behaves like the future public deployment — its own PostgreSQL database, its
own dedicated demo identities, its own container/volume/network names — so
you can exercise the whole app end to end (including per-user isolation)
without touching your real `tasks.db` or the normal `agent_data`/
`fastapi-ai-task-agent-postgres-data` volumes. See
**[docs/LOCAL_DEMO.md](docs/LOCAL_DEMO.md)** for setup, the full walkthrough,
and safe stop/reset/cleanup commands.

## Vercel + Neon compatibility

This app is prepared (not deployed) for a future zero-cost public demo on
Vercel Hobby + Neon PostgreSQL Free — a minimal entrypoint adapter
(`app/index.py`), an opt-in serverless connection-pool mode
(`DB_POOL_MODE=serverless`), and a strict separation between the runtime's
pooled Neon URL and a migration-only direct URL. No Vercel project, Neon
project, or deployment has been created. See
**[docs/VERCEL.md](docs/VERCEL.md)** for the full architecture, environment
variables, and the future (not-yet-executed) deployment procedure.

## Continuous Integration (CI)

GitHub Actions (`.github/workflows/ci.yml`) runs three independent jobs on
every push to `main`, every pull request targeting `main`, and on demand
via `workflow_dispatch`:

- **Test** — installs `requirements.txt`, runs the full `pytest` suite,
  runs the offline evaluation suite in both `rule_based` mode
  (`python -m evals.run --mode rule_based`) and `mocked-ollama` mode
  (`python -m evals.run --mode mocked-ollama`) — both fully offline, fast,
  and deterministic, so both run on every commit — then verifies the
  Alembic baseline migration (`python -m alembic upgrade head` followed by
  `python -m alembic check` against a fourth isolated SQLite database) -
  this is the automated version of "don't blindly trust autogenerated
  migration output": if `app/db_models.py` ever changes without a
  matching new revision, this step fails immediately. Uses fake,
  non-secret configuration only (`API_KEYS=ci-test-key-do-not-use:ci-user`,
  `AGENT_DECISION_PROVIDER=rule_based`, four isolated SQLite databases
  under the runner's temp directory, one per step) — your local
  `tasks.db` is never touched. **Deliberately excluded from CI**: `live-ollama`
  (no local model on a hosted runner, and pulling one on every run would
  be slow and non-deterministic) and any real Anthropic call (a paid
  external API call must never run automatically) — both stay opt-in and
  local-only, see "Evaluations" above.
- **PostgreSQL integration** — runs the Alembic migration and the opt-in
  Postgres integration tests against a real, ephemeral `postgres:16.14`
  service container scoped to this job only - see "PostgreSQL (optional)"
  below.
- **Docker build** — builds the repo's `Dockerfile` (now also copying
  `alembic.ini`/`alembic/` into the image) and tags it locally
  (`fastapi-ai-task-agent:ci`) to catch build breakage. It never pushes
  the image anywhere and needs no runtime API keys to build.

CI may download GitHub Actions, Python packages, Docker base-image
layers, and (for the PostgreSQL job only) the `postgres` image from their
respective registries, but the application tests and evals themselves
make no live calls to Ollama, Anthropic, or Open-Meteo — every job is
fully offline and deterministic with respect to those three.

Run the same checks locally:

```bash
python -m pip install -r requirements.txt
python -m pip check
python -m pytest -q
python -m evals.run --mode rule_based
python -m evals.run --mode mocked-ollama
python -m alembic upgrade head && python -m alembic check   # against a scratch DATABASE_URL, not tasks.db
docker build -t fastapi-ai-task-agent:ci .
```

Results appear on GitHub under the **Actions** tab, as check marks on
each commit, and in the "Checks" section of a pull request.

## Notes

- Tasks are persisted in SQLite (see `DATABASE_URL` above) — data survives
  server restarts. Schema is versioned with Alembic (see "Database
  migrations (Alembic)" above) — a fresh database needs `python -m
  alembic upgrade head` once before the app will start.
