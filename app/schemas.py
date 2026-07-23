"""Pydantic schemas used to validate requests and shape responses.

These are kept separate from the internal Task model (models.py) so
that what the API accepts/returns can evolve independently of how
tasks are stored internally.
"""

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskCreate(BaseModel):
    """Shape of the JSON body expected on POST /tasks."""

    title: str = Field(max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class TaskUpdate(BaseModel):
    """Shape of the JSON body expected on PATCH /tasks/{task_id}."""

    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


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

    message: str = Field(max_length=4000)


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

    message: str = Field(max_length=4000)
    conversation_id: uuid.UUID | None = None


class ClarificationOptionResponse(BaseModel):
    """One concrete candidate task offered when a title reference matches
    more than one of the user's own tasks (see app/services/task_resolution.py).
    """

    task_id: int
    title: str


class StepResultResponse(BaseModel):
    """Shape of a single entry in ExecuteResponse.steps."""

    step: int
    tool: str
    arguments: dict
    status: str
    duration_ms: int
    result: dict | list | None = None
    error: str | None = None


class ExecuteResponse(BaseModel):
    """Shape of the response for POST /agent/execute."""

    run_id: uuid.UUID
    conversation_id: uuid.UUID
    message: str
    selected_tool: str | None = None
    result: dict | list | None = None
    # The acted-on (or about-to-be-acted-on) task's title, when known
    # independently of `result` - the PRE-update title for update_task
    # (result.title already has the new one), or the target task's title
    # for mark_task_done/delete_task (delete_task's result never contains
    # a title at all). Populated whenever the task was resolved by title
    # or looked up from an explicit id (see routes/agent.py) - optional
    # presentation metadata only, never affects which task is acted on.
    # Left None for create_task (result.title already has the created
    # title), for tools with no task target, and whenever no task could
    # be found.
    resolved_task_title: str | None = None
    reason: str
    final_answer: str
    needs_clarification: bool = False
    clarification_question: str | None = None
    clarification_options: list[ClarificationOptionResponse] | None = None
    needs_confirmation: bool = False
    confirmation_question: str | None = None
    is_multi_step: bool = False
    steps: list[StepResultResponse] = Field(default_factory=list)


class AgentRunStepResponse(BaseModel):
    """Shape of a single step trace in GET /agent/runs/{run_id}."""

    model_config = ConfigDict(from_attributes=True)

    step_number: int
    tool: str
    arguments_json: str
    status: str
    duration_ms: int
    error: str | None = None
    result_summary: str | None = None


class AgentRunSummaryResponse(BaseModel):
    """Shape of a single entry in GET /agent/runs."""

    model_config = ConfigDict(from_attributes=True)

    run_id: uuid.UUID
    conversation_id: uuid.UUID
    message: str
    decision_provider: str
    is_multi_step: bool
    status: str
    selected_tool: str | None = None
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    error: str | None = None

    @field_validator("started_at", "finished_at")
    @classmethod
    def _ensure_utc(cls, value: datetime) -> datetime:
        # SQLite drops tzinfo on round-trip even for a DateTime(timezone=True)
        # column - values are always written as UTC (see
        # agent_trace_service), so a naive value read back is re-tagged as
        # UTC rather than left ambiguous.
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class AgentRunDetailResponse(AgentRunSummaryResponse):
    """Shape of the response for GET /agent/runs/{run_id} - the summary
    plus ordered step traces.
    """

    steps: list[AgentRunStepResponse] = Field(default_factory=list)
