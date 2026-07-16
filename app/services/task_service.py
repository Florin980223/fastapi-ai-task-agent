"""Business logic for tasks.

Route handlers (in routes/tasks.py) and the agent (in agent_service.py)
call these functions instead of touching SQLAlchemy directly. This
keeps the HTTP/agent layers thin and the actual persistence logic in
one place.

Every function takes user_id and scopes its query/mutation to it -
there is no way to read, update, complete, or delete a task belonging
to a different user through this module. find_task (and everything
built on it: update_task/mark_task_done/delete_task) returns None/False
for a task that doesn't exist AND for a task that exists but belongs to
someone else - the two cases are indistinguishable by design, so a
caller can never learn whether another user's task id exists.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db_models import Task


def list_tasks(db: Session, user_id: str, done: bool | None = None) -> list[Task]:
    stmt = select(Task).where(Task.user_id == user_id)
    if done is not None:
        stmt = stmt.where(Task.done == done)
    return list(db.scalars(stmt).all())


def create_task(db: Session, user_id: str, title: str, description: str | None) -> Task:
    task = Task(user_id=user_id, title=title, description=description, done=False)
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def find_task(db: Session, user_id: str, task_id: int) -> Task | None:
    stmt = select(Task).where(Task.id == task_id, Task.user_id == user_id)
    return db.scalar(stmt)


def update_task(db: Session, user_id: str, task_id: int, title: str | None, description: str | None) -> Task | None:
    task = find_task(db, user_id, task_id)
    if task is None:
        return None
    if title is not None:
        task.title = title
    if description is not None:
        task.description = description
    db.commit()
    db.refresh(task)
    return task


def mark_task_done(db: Session, user_id: str, task_id: int) -> Task | None:
    task = find_task(db, user_id, task_id)
    if task is None:
        return None
    task.done = True
    db.commit()
    db.refresh(task)
    return task


def delete_task(db: Session, user_id: str, task_id: int) -> bool:
    task = find_task(db, user_id, task_id)
    if task is None:
        return False
    db.delete(task)
    db.commit()
    return True
