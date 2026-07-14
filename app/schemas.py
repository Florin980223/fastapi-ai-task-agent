"""Pydantic schemas used to validate requests and shape responses.

These are kept separate from the internal Task model (models.py) so
that what the API accepts/returns can evolve independently of how
tasks are stored internally.
"""

from pydantic import BaseModel


class TaskCreate(BaseModel):
    """Shape of the JSON body expected on POST /tasks."""

    title: str
    description: str | None = None


class TaskResponse(BaseModel):
    """Shape of a task as returned by the API."""

    id: int
    title: str
    description: str | None = None
    done: bool
