# FastAPI AI Task Agent

A small FastAPI backend for managing tasks, built as a learning project.
This first version uses in-memory storage (no database) and has no AI
integration yet — that comes later.

## Setup

```bash
python -m venv venv
venv\Scripts\activate      # on Windows
pip install -r requirements.txt
```

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

- Data is stored in memory only — restarting the server clears all tasks.
- No database and no AI agent yet; both are planned for later phases.
