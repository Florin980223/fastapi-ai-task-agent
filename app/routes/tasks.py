"""HTTP endpoints for tasks.

Each route only handles request/response wiring (input validation via
schemas, HTTP status codes/errors) and delegates the real work to
app.services.task_service. Every route requires X-API-Key auth and
scopes its call to the authenticated user - see app/services/auth.py.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import TaskCreate, TaskResponse, TaskUpdate
from app.services import task_service
from app.services.auth import AuthenticatedUser, get_current_user

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskResponse])
def get_tasks(done: bool | None = None, db: Session = Depends(get_db), current_user: AuthenticatedUser = Depends(get_current_user)):
    return task_service.list_tasks(db, current_user.user_id, done=done)


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
def create_task(task_in: TaskCreate, db: Session = Depends(get_db), current_user: AuthenticatedUser = Depends(get_current_user)):
    return task_service.create_task(
        db,
        current_user.user_id,
        title=task_in.title,
        description=task_in.description,
        priority=task_in.priority,
        due_date=task_in.due_date,
    )


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(task_id: int, db: Session = Depends(get_db), current_user: AuthenticatedUser = Depends(get_current_user)):
    task = task_service.find_task(db, current_user.user_id, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.patch("/{task_id}", response_model=TaskResponse)
def update_task(
    task_id: int,
    task_in: TaskUpdate,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    # due_date is genuinely tri-state (omitted = unchanged, explicit null
    # = clear) - only pass it through at all when the request actually
    # included the key, so task_service.update_task's _UNSET sentinel
    # default correctly means "leave unchanged" for every other caller.
    due_date_kwargs = {}
    if "due_date" in task_in.model_fields_set:
        due_date_kwargs["due_date"] = task_in.due_date

    task = task_service.update_task(
        db,
        current_user.user_id,
        task_id,
        title=task_in.title,
        description=task_in.description,
        priority=task_in.priority,
        **due_date_kwargs,
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.patch("/{task_id}/done", response_model=TaskResponse)
def mark_task_done(task_id: int, db: Session = Depends(get_db), current_user: AuthenticatedUser = Depends(get_current_user)):
    task = task_service.mark_task_done(db, current_user.user_id, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(task_id: int, db: Session = Depends(get_db), current_user: AuthenticatedUser = Depends(get_current_user)):
    deleted = task_service.delete_task(db, current_user.user_id, task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Task not found")
