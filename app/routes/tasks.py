"""HTTP endpoints for tasks.

Each route only handles request/response wiring (input validation via
schemas, HTTP status codes/errors) and delegates the real work to
app.services.task_service.
"""

from fastapi import APIRouter, HTTPException, status

from app.schemas import TaskCreate, TaskResponse, TaskUpdate
from app.services import task_service

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskResponse])
def get_tasks():
    return task_service.list_tasks()


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
def create_task(task_in: TaskCreate):
    return task_service.create_task(title=task_in.title, description=task_in.description)


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(task_id: int):
    task = task_service.find_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.patch("/{task_id}", response_model=TaskResponse)
def update_task(task_id: int, task_in: TaskUpdate):
    task = task_service.update_task(task_id, title=task_in.title, description=task_in.description)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.patch("/{task_id}/done", response_model=TaskResponse)
def mark_task_done(task_id: int):
    task = task_service.mark_task_done(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(task_id: int):
    deleted = task_service.delete_task(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Task not found")
