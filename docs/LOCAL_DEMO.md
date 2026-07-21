# Local production-like demo

## Purpose

A safe, isolated, zero-cost local environment that behaves like the future
public deployment: PostgreSQL-backed, single worker, real explicit startup
order (migrate → start → wait for `/ready`), and dedicated demo identities
whose data isolation you can actually see and prove. It exists to let you
(or a future collaborator) exercise the whole app the way it will eventually
run in public, without touching your own dev data, and to give the eventual
public-deployment work a proven, working reference to build from.

## Difference from normal SQLite development

The bare `python -m venv` / `uvicorn app.main:app --reload` workflow (see the
main [README.md](../README.md)) and the default `docker compose up` workflow
both use SQLite and your own dev identity by default. This demo is a
**separate, standalone Compose stack** (`compose.demo.yaml`) with its own
PostgreSQL database, its own container/volume/network names, its own port
numbers, and two dedicated demo identities (`demo_user_a`/`demo_user_b`) -
it never uses `tasks.db`, never uses the `agent_data`/
`fastapi-ai-task-agent-postgres-data` volumes, and never uses your real API
key or user_id.

## Prerequisites

- Docker Desktop running (the same requirement as the optional PostgreSQL
  profile in the main README).
- Python (for running `docker/demo_seed.py` from the host, and for Alembic
  if you ever want to run it outside a container - not required for normal
  use, `docker/demo_start.ps1` handles this for you).
- Windows PowerShell 5.1 or later (all scripts here match
  `docker/smoke_test.ps1`'s PS 5.1-compatible conventions).
- Nothing else. See "Zero-cost statement" below.

## One-time setup

```powershell
Copy-Item .env.demo.example .env.demo
```

Then edit `.env.demo` and replace the placeholders:
- `POSTGRES_PASSWORD` - any random string.
- `API_KEYS` - two dedicated demo keys (see "Generating the demo API key"
  below) mapped to `demo_user_a` and `demo_user_b` - both identities are
  needed for the isolation checks in `docker/demo_verify.ps1`.

`.env.demo` is gitignored and dockerignored - it never gets committed and is
never sent into any Docker build context. `docker/demo_start.ps1` fails
clearly if it's missing.

## Generating the demo API key

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Run this twice (once per demo identity) and paste each result directly into
`.env.demo`'s `API_KEYS=` line - never into a chat message, a script
argument you'll leave in shell history, or anywhere else. To get a key from
`.env.demo` into the Web UI without ever displaying it:

```powershell
$line = (Get-Content .env.demo | Where-Object { $_ -match '^API_KEYS=' })
$keyA = (($line -split '=', 2)[1] -split ',')[0] -split ':' | Select-Object -First 1
Set-Clipboard -Value $keyA
Remove-Variable line, keyA
```

Then paste (Ctrl+V) into the Web UI's "API key" field. When you're done with
the demo session, click "Clear API key" in the Web UI (clears
`sessionStorage`) and overwrite your clipboard so the key doesn't linger
there:

```powershell
"cleared" | Set-Clipboard
```

## Startup order

```powershell
.\docker\demo_start.ps1
```

Does exactly this, in order, and is safe to re-run at any time:
1. Checks `compose.demo.yaml` and `.env.demo` exist.
2. Checks host ports `8100`/`55434` are available (or already owned by this
   exact demo stack, in which case it's not a conflict).
3. Starts `postgres-demo` and waits for it to become healthy.
4. Runs `python -m alembic upgrade head` explicitly, inside a one-off
   `app-demo` container - **never automatic**, matching the same
   verify-only startup contract the main app uses everywhere.
5. Starts `app-demo`.
6. Polls `GET /ready` until it returns `200`.
7. Prints the Web UI URL.

## Web UI URL

**http://localhost:8100/**

## Complete demo walkthrough

1. Open http://localhost:8100/, paste a demo API key (see above), click
   "Save key".
2. **Tasks tab**: create a task, confirm it appears in the list, click
   "Edit" and change its title, click "Mark done" (confirm strikethrough +
   button relabels to "Mark not done"), click "Delete" (confirm the "Delete
   this task? This cannot be undone." dialog appears), confirm deletion.
3. **Agent tab**: type `create a task` (no title) and send - the agent
   responds "What should the task title be?" (clarification). Reply with a
   title - the task is created.
4. Type `delete task <id>` for a task you own - the agent responds "Are you
   sure you want to delete task #<id>?" (destructive confirmation). Reply
   `yes` - the task is deleted. (Reply `no` instead to test the
   cancellation path - nothing is deleted.)
5. **Run History tab**: confirm both runs above are listed with their
   status (`clarification_required`/`success`,
   `confirmation_required`/`success`); click into one to see step-level
   detail (tool, duration, result).
6. Click "Clear API key" - confirms `sessionStorage` was cleared and you're
   returned to the key-entry screen.

This exact flow was verified against a live instance during implementation
of this feature (rule_based provider, fully offline).

## Persistence test

```powershell
# create some data first (via the Web UI or docker/demo_verify.ps1), then:
docker compose --env-file .env.demo -f compose.demo.yaml -p fastapi-ai-task-agent-demo stop
docker compose --env-file .env.demo -f compose.demo.yaml -p fastapi-ai-task-agent-demo start
# or simply:
.\docker\demo_start.ps1
```

`stop` never removes the volume or network - only the containers.
Afterward, confirm your tasks, conversation state, and run history are all
still present (`GET /tasks`, `GET /agent/runs`). This was verified during
implementation: a task and a full agent-run history survived a stop/start
cycle unchanged.

## Optional: seed fictional demo data

```powershell
$line = (Get-Content .env.demo | Where-Object { $_ -match '^POSTGRES_PASSWORD=' })
$pw = ($line -split '=', 2)[1]
$env:DEMO_DATABASE_URL = "postgresql+psycopg://taskagent_demo:$pw@localhost:55434/taskagent_demo"

python docker\demo_seed.py --user-id demo_user_a --dry-run   # preview only, writes nothing
python docker\demo_seed.py --user-id demo_user_a             # actually inserts

Remove-Item Env:\DEMO_DATABASE_URL
Remove-Variable line, pw
```

Inserts a small fixed set of fictional, non-sensitive task titles ("Buy
milk", "Plan demo walkthrough", ...) for the given user - idempotent (safe
to re-run, never duplicates, never touches a pre-existing row), and refuses
to run against anything that isn't exactly the isolated demo database
(`taskagent_demo` on `localhost`) and an allowlisted demo user_id
(`demo_user_a`/`demo_user_b`). See `docker/demo_seed.py`'s own docstring for
the full list of guards.

## Safe stop

```powershell
docker compose --env-file .env.demo -f compose.demo.yaml -p fastapi-ai-task-agent-demo stop
```

Stops both containers cleanly (verified graceful exit, code 0). Preserves
all data - the named volume is untouched.

## Safe reset (wipe only the demo data)

```powershell
.\docker\demo_reset_data.ps1          # dry run - prints what would happen, changes nothing
.\docker\demo_reset_data.ps1 -Force   # actually resets
```

Removes and recreates **only** the `fastapi-ai-task-agent-demo-postgres-data`
volume (verified by its exact name and its Docker Compose project label
before touching it), then re-migrates and restarts the stack automatically.
This wipes all demo data, including anything `docker/demo_seed.py` inserted
- that's the point. Never touches `agent_data` or
`fastapi-ai-task-agent-postgres-data` (a completely separate Compose
project). Never runs `docker compose down -v`.

## Safe full cleanup

```powershell
.\docker\demo_cleanup.ps1                     # dry run
.\docker\demo_cleanup.ps1 -Force              # removes demo containers + network, KEEPS the data volume
.\docker\demo_cleanup.ps1 -Force -RemoveData  # also removes the demo data volume
```

Every removal is preceded by a Docker Compose project-label check on the
exact named resource - if a label is missing or doesn't match
`fastapi-ai-task-agent-demo`, the script refuses and exits with an error
rather than proceeding. Never `docker compose down -v`, never a
wildcard/pattern-based removal, never `docker system prune`.

## Verifying the demo end-to-end

```powershell
.\docker\demo_verify.ps1 -BaseUrl http://localhost:8100 -DemoApiKeyA <key-a> -DemoApiKeyB <key-b>
```

Reuses the existing `docker/smoke_test.ps1 -Full` unmodified for baseline
checks, then proves: identity A's tasks/runs are invisible to identity B and
vice versa (404, not 403), the clarification flow, and the full
destructive-confirmation flow (trigger → confirm → verify deleted). All
three required parameters are explicit - the script refuses to run with
anything unspecified.

## Troubleshooting

- **`demo_start.ps1` fails with "port already in use"**: it names the exact
  port and, where safely knowable, the conflicting container's name or
  process id. It never stops/removes anything automatically - free the port
  yourself (or, if it says the conflict is this same demo stack, that's not
  actually an error - just re-run).
- **`.env.demo not found`**: run the one-time setup above.
- **`GET /ready` returns `503`**: see the main
  [docs/DEPLOYMENT.md](DEPLOYMENT.md)'s "Health vs. readiness" /
  "Troubleshooting" sections - the same semantics apply here, just against
  `postgres-demo`/`taskagent_demo` instead of a real deployment's database.
- **Alembic migration fails**: check `docker compose --env-file .env.demo -f
  compose.demo.yaml -p fastapi-ai-task-agent-demo logs postgres-demo` -
  usually a `.env.demo` misconfiguration (mismatched `POSTGRES_PASSWORD` vs.
  `DATABASE_URL`).
- **Want a completely fresh start**: `docker/demo_cleanup.ps1 -Force
  -RemoveData` then `docker/demo_start.ps1`.

## Security limitations

- This is still entirely local - no TLS, no real perimeter security, not a
  deployment.
- API-key entry via `sessionStorage` (the Web UI's actual auth pattern) is a
  demo pattern, not a production-grade credential store - see README.md's
  "Local-demo security limitations" section, which applies identically here.
- Single worker remains required (same reasons as everywhere else in this
  project - the in-memory rate limiter and conversation-state confirmation
  atomicity are process-local).
- Treat the demo API keys as disposable - regenerate them if you're ever
  unsure whether they leaked.
- All demo data is fictional and non-sensitive by construction (`
  docker/demo_seed.py`'s fixed title list, or whatever you type by hand).
- This demo never exposes, reads, or connects to your personal database
  (`tasks.db`) in any way.
- No production availability, backup, or uptime guarantee of any kind - this
  is a local, throwaway environment you can reset or delete at any time.

## Zero-cost statement

Everything here runs entirely on your own machine using Docker Desktop, the
official `postgres:16.14` image, this repository's own code, and the
`rule_based` decision provider (no external API calls, no API key beyond the
ones you generate yourself for local auth). No cloud resources, no paid
services, no domain, no credit card, and no external database are required
or used anywhere in this feature.

## Transition to a future public deployment

This demo's shape - explicit ordered startup, `/ready`-gated readiness,
single worker, isolated PostgreSQL, dedicated non-root identities - is
deliberately the same shape a real public deployment will need (see
[docs/DEPLOYMENT.md](DEPLOYMENT.md)). This feature intentionally does not
pick or configure a cloud provider, container orchestrator, or hosting
service - it stops at "a working, provable, local reference" so that
whichever platform is chosen later can be validated against known-good
behavior rather than guessed at from scratch.
