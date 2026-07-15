"""Pydantic schemas used to validate requests and shape responses.

These are kept separate from the internal Task model (models.py) so
that what the API accepts/returns can evolve independently of how
tasks are stored internally.
"""

import uuid

from pydantic import BaseModel, ConfigDict


class TaskCreate(BaseModel):
    """Shape of the JSON body expected on POST /tasks."""

    title: str
    description: str | None = None


class TaskUpdate(BaseModel):
    """Shape of the JSON body expected on PATCH /tasks/{task_id}."""

    title: str | None = None
    description: str | None = None


class TaskResponse(BaseModel):
    """Shape of a task as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str | None = None
    done: bool


class WeatherResponse(BaseModel):
    """Shape of the response for GET /integrations/weather."""

    city: str
    country: str | None = None
    latitude: float
    longitude: float
    current_temperature: float
    wind_speed: float
    weather_code: int


class ToolResponse(BaseModel):
    """Shape of a single entry in the GET /agent/tools list."""

    name: str
    description: str
    method: str
    endpoint: str


class DecideToolRequest(BaseModel):
    """Shape of the JSON body expected on POST /agent/decide-tool."""

    message: str


class DecideToolResponse(BaseModel):
    """Shape of the response for POST /agent/decide-tool."""

    message: str
    selected_tool: str | None = None
    reason: str


class ExecuteRequest(BaseModel):
    """Shape of the JSON body expected on POST /agent/execute.

    conversation_id is optional: omit it for a fresh conversation (one
    will be generated and returned), or pass back the one from a
    previous response to continue answering a pending clarification.
    """

    message: str
    conversation_id: uuid.UUID | None = None


class ExecuteResponse(BaseModel):
    """Shape of the response for POST /agent/execute."""

    conversation_id: uuid.UUID
    message: str
    selected_tool: str | None = None
    result: dict | list | None = None
    reason: str
    final_answer: str
    needs_clarification: bool = False
    clarification_question: str | None = None
    needs_confirmation: bool = False
    confirmation_question: str | None = None
