"""The shared internal contract for multi-step planning.

Kept in its own tiny module (mirrors tool_decision.py's role for single-step
decisions) so both the planner dispatch/validation code (agent_planner.py)
and provider-specific planning code (ollama_planner_provider.py) can import
it without importing each other.
"""

from typing import Literal

from pydantic import BaseModel, Field

from app.config import AGENT_MAX_PLAN_STEPS


class PlannedStep(BaseModel):
    """One step of a plan, as produced by a planner - untrusted until
    agent_planner.validates it.
    """

    tool: str
    arguments: dict[str, str | int | bool | None] = Field(default_factory=dict)
    # 1-based index of an earlier step whose returned task id this step's
    # task_id should use. The ONLY supported reference - no expressions,
    # no other fields. Mutually exclusive with arguments["task_id"]: a
    # task id must come from exactly one source (enforced in
    # agent_planner, since it's a cross-field business rule, not a shape
    # constraint).
    task_id_from_step: int | None = None


class AgentPlan(BaseModel):
    """An ordered sequence of tool steps, 2 to AGENT_MAX_PLAN_STEPS long.

    min_length=2 means a "plan" of 0 or 1 steps is rejected as early as
    possible (at JSON-parse time in ollama_planner_provider.plan) rather
    than ever being treated as a valid multi-step plan - a 1-step request
    is a single-step request, full stop. max_length is the configured
    maximum (app.config.AGENT_MAX_PLAN_STEPS, default 3) - also re-checked
    independently in agent_planner._validate_plan as defense in depth, in
    case a plan is ever constructed some other way.
    """

    steps: list[PlannedStep] = Field(min_length=2, max_length=AGENT_MAX_PLAN_STEPS)


class StepResult(BaseModel):
    """The outcome of executing (or refusing to execute) one planned step.

    status meanings:
    - "success": executed, the tool returned a normal result.
    - "error": executed, but the tool's own result contained an "error"
      key (e.g. task not found) - same convention agent_service already
      uses.
    - "stopped": never executed at all, because a pre-flight check
      (missing argument, confirmation required, invalid reference target)
      failed.

    "error" and "stopped" both halt the plan - the halting step is always
    reported, never silently skipped.
    """

    step: int
    tool: str
    arguments: dict[str, str | int | bool | None]
    status: Literal["success", "error", "stopped"]
    duration_ms: int
    result: dict | list | None = None
    error: str | None = None
