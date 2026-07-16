# FastAPI AI Task Agent

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

## Notes

- Tasks are persisted in SQLite (see `DATABASE_URL` above) — data survives
  server restarts. Table creation happens automatically on startup; there
  are no migrations yet (schema changes require recreating the database).
