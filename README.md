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

## Notes

- Tasks are persisted in SQLite (see `DATABASE_URL` above) — data survives
  server restarts. Table creation happens automatically on startup; there
  are no migrations yet (schema changes require recreating the database).
