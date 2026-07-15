"""Multi-step plan dispatch, structural validation, and sequential execution.

Mirrors agent_decision.py's role for single-step decisions: this module
owns "should we even try planning" and "is this plan safe", while
ollama_planner_provider.py owns the Ollama-specific prompt/payload/parsing.
No network code lives here, and no tool ever runs anywhere except through
agent_service.execute_tool - the same execution layer single-step decisions
already use.

Two outcomes are deliberately kept distinct rather than collapsed into one
"None means try something else":
- should_attempt_planning() is a pure gate: False means multi-step
  planning simply doesn't apply to this message right now (disabled, wrong
  provider, or no multi-step cue in the message) - the caller should use
  the ordinary single-step pipeline, exactly as if this module didn't
  exist.
- decide_plan() should only be called once the gate is True. None from it
  means planning was attempted and failed, or produced an invalid/unsafe
  plan - the caller must NOT fall back to single-step execution of the
  original (possibly multi-action) message, since that could silently
  execute only a fragment of what was asked. It should return a controlled,
  no-execution response instead.
"""

import logging
import re
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import MULTI_STEP_PLANNING_ENABLED
from app.services import agent_decision, agent_service, clarification, conversation_memory, ollama_planner_provider, tool_schemas
from app.services.agent_plan import AgentPlan, PlannedStep, StepResult
from app.services.tool_decision import ToolDecision

logger = logging.getLogger(__name__)


# Deterministic, scope-limiting cues only - never a safety mechanism.
# Getting these wrong just means a message does or doesn't get a planning
# attempt; it can never cause a wrong execution, since every plan is fully
# validated (and every step re-validated) regardless of how it was reached.
_ENGLISH_CUES = ["then", "after that"]
_ROMANIAN_CUES = ["și apoi", "iar apoi", "apoi", "după aceea"]
_MULTI_STEP_CUES = _ENGLISH_CUES + _ROMANIAN_CUES


def looks_multi_step(message: str) -> bool:
    """Whether the message contains an explicit cue that it's asking for
    more than one action, in English or Romanian.

    Case-insensitive and word-boundary aware (matches
    clarification.mentions_contextual_reference's approach), so this
    correctly handles Romanian diacritics (ș/ă) and never matches a cue as
    part of an unrelated word.
    """
    lowered = message.lower()
    return any(re.search(r"\b" + re.escape(cue) + r"\b", lowered) for cue in _MULTI_STEP_CUES)


_CUE_SPLIT_PATTERN = r"\b(?:" + "|".join(re.escape(cue) for cue in _MULTI_STEP_CUES) + r")\b"


def _split_into_clauses(message: str) -> list[str]:
    """Split a message into clauses at the same multi-step cue boundaries
    looks_multi_step uses, so each clause can be checked for destructive
    intent independently rather than scanning the whole message as one
    blob (see _has_destructive_intent).
    """
    lowered = message.lower()
    return [clause.strip() for clause in re.split(_CUE_SPLIT_PATTERN, lowered) if clause.strip()]


# Explicit, deterministic destructive delete/remove phrases (English and
# Romanian). Specific action phrases, not the bare word "delete" - this is
# what lets a task title like "delete old files" pass through untouched
# while "delete it"/"delete task"/etc. as their own clause do not.
_DESTRUCTIVE_INTENT_PHRASES = {
    "delete it",
    "delete task",
    "remove it",
    "remove that task",
    "erase task",
    "șterge-l",
    "sterge-l",
    "șterge task-ul",
    "elimină task-ul",
}


def _has_destructive_intent(message: str) -> bool:
    """Deterministic pre-planning safety guard: whether any clause of a
    multi-step message expresses an explicit delete/remove intent.

    delete_task is excluded from the planner's tool allowlist, but that
    alone isn't sufficient: a small model asked to plan a request with
    destructive intent may silently substitute a different allowed tool
    instead of refusing outright (observed live: "...and then delete it"
    was planned as mark_task_done). This check stops the request before
    the model is ever consulted, using the same boundary-safe,
    case-insensitive, English/Romanian phrase matching approach as
    clarification.mentions_contextual_reference - never a naive substring
    check.

    Splitting into clauses first (see _split_into_clauses) keeps a task
    title that happens to contain the word "delete" ("Create a task
    called delete old files and then show my tasks") from being confused
    with an actual destructive clause ("...and then delete it").
    """
    for clause in _split_into_clauses(message):
        if any(re.search(r"\b" + re.escape(phrase) + r"\b", clause) for phrase in _DESTRUCTIVE_INTENT_PHRASES):
            return True
    return False


def should_attempt_planning(message: str) -> bool:
    """Gate only - whether to try planning at all for this message.

    Reads agent_decision.DECISION_PROVIDER live (not its own copy) so
    tests only ever need to monkeypatch agent_decision.DECISION_PROVIDER
    in one place, exactly as they already do for single-step tests.
    """
    return (
        MULTI_STEP_PLANNING_ENABLED
        and agent_decision.DECISION_PROVIDER == "ollama"
        and looks_multi_step(message)
    )


# The only tools a plan may use. delete_task is a known, executable tool
# elsewhere in the app but is never valid inside a multi-step plan - a
# destructive action must still go through the existing single-step
# confirmation flow (see routes/agent.py), which multi-step plans don't
# have a way to pause and resume for yet.
_ALLOWED_PLANNER_TOOLS = {"create_task", "list_tasks", "get_weather", "mark_task_done", "update_task"}

# Tools whose successful result is a dict with an integer "id" - the only
# legal targets for a task_id_from_step reference.
_TASK_IDENTIFYING_TOOLS = {"create_task", "update_task", "mark_task_done"}


def _validate_plan(raw: AgentPlan) -> AgentPlan | None:
    """Structural/semantic validation of a freshly-parsed plan, run once
    before any execution. Returns None (reject the whole plan) if anything
    here looks like a planner malfunction rather than a legitimate runtime
    condition.
    """
    steps = raw.steps

    # Defense in depth - AgentPlan.steps already enforces this via
    # Field(min_length=2, max_length=3), but a plan could in principle be
    # constructed some other way.
    if len(steps) not in (2, 3):
        return None

    for index, step in enumerate(steps, start=1):
        if step.tool not in _ALLOWED_PLANNER_TOOLS:
            return None

        has_explicit_task_id = step.arguments.get("task_id") is not None
        has_reference = step.task_id_from_step is not None

        if has_explicit_task_id and has_reference:
            # A task id must come from exactly one source - never let one
            # silently win over the other.
            return None

        if has_reference:
            target_index = step.task_id_from_step
            if target_index is None or target_index < 1 or target_index >= index:
                # No self- or forward-references, and the index must
                # actually point at an earlier step.
                return None

            target_step = steps[target_index - 1]
            if target_step.tool not in _TASK_IDENTIFYING_TOOLS:
                return None

            if "task_id" not in tool_schemas.REQUIRED_ARGUMENTS.get(step.tool, {}):
                return None

    return raw


def decide_plan(message: str) -> AgentPlan | None:
    """Only call once should_attempt_planning(message) is True.

    None means planning was attempted and failed, or produced an
    invalid/unsafe plan - see the module docstring for what the caller
    must (and must not) do with that.
    """
    if _has_destructive_intent(message):
        logger.warning("Refusing to plan a message with destructive delete/remove intent.")
        return None

    try:
        raw_plan = ollama_planner_provider.plan(message)
    except ollama_planner_provider.OllamaPlanningError as exc:
        logger.warning("Ollama planning failed (%s).", exc)
        return None

    return _validate_plan(raw_plan)


def _resolve_step_arguments(step: PlannedStep, results: list[StepResult]) -> dict | None:
    """Resolve a step's final arguments, filling in task_id from an earlier
    step's result if task_id_from_step is set.

    Returns None if the reference can't be resolved at runtime (the
    referenced step didn't actually succeed, or didn't return an integer
    id) - the one thing _validate_plan can't know upfront. _validate_plan
    already guarantees a step never has both an explicit task_id and a
    task_id_from_step, so this merge can never silently clobber an
    explicit value.
    """
    if step.task_id_from_step is None:
        return dict(step.arguments)

    referenced = results[step.task_id_from_step - 1]
    if referenced.status != "success" or not isinstance(referenced.result, dict):
        return None

    task_id = referenced.result.get("id")
    if not isinstance(task_id, int):
        return None

    arguments = dict(step.arguments)
    arguments["task_id"] = task_id
    return arguments


def execute_plan(plan: AgentPlan, db: Session, conversation_id: UUID) -> list[StepResult]:
    """Run a validated plan's steps, strictly in order, stopping at the
    first step that isn't safe or successful to run.

    Every step still runs through agent_service.execute_tool - the same
    execution layer single-step decisions use. No retries, no skipping: a
    step that fails or can't be safely run is always reported, and nothing
    after it executes.
    """
    results: list[StepResult] = []

    for index, step in enumerate(plan.steps, start=1):
        arguments = _resolve_step_arguments(step, results)
        if arguments is None:
            results.append(
                StepResult(
                    step=index,
                    tool=step.tool,
                    arguments=step.arguments,
                    status="stopped",
                    error="Could not resolve the referenced task id from an earlier step.",
                )
            )
            break

        decision = ToolDecision(selected_tool=step.tool, arguments=arguments, reason="Planned step.")

        missing = clarification.missing_arguments(decision)
        if missing:
            results.append(
                StepResult(
                    step=index,
                    tool=step.tool,
                    arguments=arguments,
                    status="stopped",
                    error=f"Missing required argument(s): {', '.join(missing)}.",
                )
            )
            break

        if clarification.requires_confirmation(step.tool):
            results.append(
                StepResult(
                    step=index,
                    tool=step.tool,
                    arguments=arguments,
                    status="stopped",
                    error=f"'{step.tool}' requires confirmation and cannot run inside a multi-step plan.",
                )
            )
            break

        try:
            tool_schemas.validate_tool_call(step.tool, arguments)
        except tool_schemas.ToolCallValidationError as exc:
            results.append(
                StepResult(step=index, tool=step.tool, arguments=arguments, status="stopped", error=str(exc))
            )
            break

        result = agent_service.execute_tool(decision, db)
        conversation_memory.record_result(conversation_id, step.tool, result)

        if isinstance(result, dict) and "error" in result:
            results.append(
                StepResult(step=index, tool=step.tool, arguments=arguments, status="error", result=result, error=result["error"])
            )
            break

        results.append(StepResult(step=index, tool=step.tool, arguments=arguments, status="success", result=result))

    return results


def build_plan_reason(steps: list[StepResult], planned_count: int) -> str:
    """Deterministic, generic explanation of what a plan execution did."""
    if len(steps) == planned_count and all(step.status == "success" for step in steps):
        return f"Executed a {planned_count}-step plan."
    return f"Stopped a {planned_count}-step plan after step {len(steps)}."


def build_plan_final_answer(steps: list[StepResult]) -> str:
    """Human-readable summary of a plan execution, one sentence per step."""
    sentences = []
    for step in steps:
        if step.status in ("success", "error"):
            sentences.append(agent_service.generate_final_answer(step.tool, step.result))
        else:
            sentences.append(f"Stopped before running {step.tool}: {step.error}")
    return " ".join(sentences)
