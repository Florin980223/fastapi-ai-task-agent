"""Business logic for tasks.

Route handlers (in routes/tasks.py) call these functions instead of
touching the in-memory store directly. This keeps the HTTP layer thin
and the actual logic in one place.
"""

from app.models import Task, tasks_db, get_next_id


def list_tasks() -> list[Task]:
    return tasks_db


def create_task(title: str, description: str | None) -> Task:
    task = Task(id=get_next_id(), title=title, description=description, done=False)
    tasks_db.append(task)
    return task


def find_task(task_id: int) -> Task | None:
    for task in tasks_db:
        if task.id == task_id:
            return task
    return None


def update_task(task_id: int, title: str | None, description: str | None) -> Task | None:
    task = find_task(task_id)
    if task is None:
        return None
    if title is not None:
        task.title = title
    if description is not None:
        task.description = description
    return task


def mark_task_done(task_id: int) -> Task | None:
    task = find_task(task_id)
    if task is None:
        return None
    task.done = True
    return task


def delete_task(task_id: int) -> bool:
    task = find_task(task_id)
    if task is None:
        return False
    tasks_db.remove(task)
    return True
