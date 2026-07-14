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


class TaskUpdate(BaseModel):
    """Shape of the JSON body expected on PATCH /tasks/{task_id}."""

    title: str | None = None
    description: str | None = None


class TaskResponse(BaseModel):
    """Shape of a task as returned by the API."""

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


class ExecuteResponse(BaseModel):
    """Shape of the response for POST /agent/execute."""

    message: str
    selected_tool: str | None = None
    result: dict | list | None = None
    reason: str
    final_answer: str
