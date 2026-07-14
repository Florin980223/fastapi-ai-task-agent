"""Business logic for tasks.

Route handlers (in routes/tasks.py) and the agent (in agent_service.py)
call these functions instead of touching SQLAlchemy directly. This
keeps the HTTP/agent layers thin and the actual persistence logic in
one place.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db_models import Task


def list_tasks(db: Session, done: bool | None = None) -> list[Task]:
    stmt = select(Task)
    if done is not None:
        stmt = stmt.where(Task.done == done)
    return list(db.scalars(stmt).all())


def create_task(db: Session, title: str, description: str | None) -> Task:
    task = Task(title=title, description=description, done=False)
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def find_task(db: Session, task_id: int) -> Task | None:
    return db.get(Task, task_id)


def update_task(db: Session, task_id: int, title: str | None, description: str | None) -> Task | None:
    task = find_task(db, task_id)
    if task is None:
        return None
    if title is not None:
        task.title = title
    if description is not None:
        task.description = description
    db.commit()
    db.refresh(task)
    return task


def mark_task_done(db: Session, task_id: int) -> Task | None:
    task = find_task(db, task_id)
    if task is None:
        return None
    task.done = True
    db.commit()
    db.refresh(task)
    return task


def delete_task(db: Session, task_id: int) -> bool:
    task = find_task(db, task_id)
    if task is None:
        return False
    db.delete(task)
    db.commit()
    return True
