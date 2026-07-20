"""A local-Ollama-powered planner for multi-step agent execution.

Mirrors ollama_decision_provider.py's shape, but produces a whole ordered
AgentPlan (up to 3 existing-tool steps) instead of a single tool call.
Ollama is only ever asked to select from the app's own tools via a
JSON-Schema-constrained structured response; it cannot run arbitrary code,
shell commands, SQL, or HTTP requests. The caller (agent_planner.decide_plan)
is responsible for treating any failure here as "planning failed" - never as
a signal to fall back to single-step execution of the original message.

Deliberately does NOT reuse ollama_decision_provider._call_ollama, even
though the HTTP body is nearly identical: keeping the planning and
single-step decision HTTP seams separate means tests (and the code) can
mock/observe them completely independently. Same reasoning
clarification.py already gives for its own small duplicated regex helper
rather than importing across a module-privacy boundary.
"""

import json
import logging
import time

import httpx
from pydantic import ValidationError

from app.config import AGENT_MAX_PLAN_STEPS, OLLAMA_BASE_URL, OLLAMA_MODEL, OLLAMA_TIMEOUT_SECONDS
from app.services import decision_validation
from app.services.agent_plan import AgentPlan

logger = logging.getLogger(__name__)

# Built from AGENT_MAX_PLAN_STEPS rather than a hardcoded "2 or 3" so the
# prompt can never drift from AgentPlan's own configured bound.
_PLANNING_SYSTEM_PROMPT = (
    "You are the planning layer of a small task-management assistant.\n\n"
    f"Given the user's message, break it down into an ordered plan of 2 to "
    f"{AGENT_MAX_PLAN_STEPS} steps, where each step calls exactly one of these tools:\n"
    "- create_task(title: string) - create a new task.\n"
    "- list_tasks(done: boolean, optional) - list tasks, optionally filtered "
    "by completion status.\n"
    "- get_weather(city: string) - get the current weather for a city.\n"
    "- mark_task_done(task_id: integer) - mark a task as completed.\n"
    "- update_task(task_id: integer, title: string) - rename a task.\n\n"
    "delete_task is NEVER available here - do not use it under any "
    "circumstances, even if the user asks to delete something. If the "
    "user's request requires deleting a task, do not produce a plan for it "
    "at all.\n\n"
    "If a later step needs the id of a task that an earlier step in this "
    "same plan creates or identifies, set that step's \"task_id_from_step\" "
    "to the 1-based number of the earlier step, and leave \"task_id\" out "
    "of that step's arguments entirely - never guess a task id and never "
    "set both task_id and task_id_from_step on the same step.\n\n"
    f"Only ever produce between 2 and {AGENT_MAX_PLAN_STEPS} steps. If the "
    "user's message doesn't clearly need more than one tool, or you cannot "
    f"break it into a safe ordered plan using only the tools above, still "
    f"return your best 2-to-{AGENT_MAX_PLAN_STEPS}-step attempt using only "
    "those tools - the caller will validate it.\n\n"
    "Example - \"Create a task to buy milk and then show me all tasks\":\n"
    '{"steps": [{"tool": "create_task", "arguments": {"title": "buy milk"}}, '
    '{"tool": "list_tasks", "arguments": {}}]}\n\n'
    "Example - \"Add a task to call Alex and then mark it done\":\n"
    '{"steps": [{"tool": "create_task", "arguments": {"title": "call Alex"}}, '
    '{"tool": "mark_task_done", "arguments": {}, "task_id_from_step": 1}]}\n\n'
    "Respond only with the JSON plan in the provided schema - never plain prose."
)

class OllamaPlanningError(decision_validation.DecisionProviderError):
    """Raised whenever the Ollama planner can't produce a trustworthy plan.

    Covers the server being unreachable/slow, invalid JSON, and a plan that
    fails Pydantic validation (including the wrong number of steps) -
    agent_planner.decide_plan catches this one type and treats it as
    "planning failed", never as a signal to fall back to single-step
    execution of the original message.
    """


def _call_ollama_plan(payload: dict) -> dict:
    """POST to Ollama's /api/chat and return the parsed JSON response.

    Kept as its own function (separate from
    ollama_decision_provider._call_ollama) so tests can monkeypatch it
    instead of making a real HTTP call.
    """
    response = httpx.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload, timeout=OLLAMA_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def _build_payload(messages: list[dict]) -> dict:
    return {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "think": False,
        # Ollama structured-output mode: constrains the model's JSON to
        # this schema. This is a best-effort hint only - agent_planner
        # fully re-validates every field afterward regardless.
        "format": AgentPlan.model_json_schema(),
        "options": {"temperature": 0},
    }


def _parse_plan(content: str | None) -> AgentPlan:
    """Parse and validate a plan's raw JSON content. Raises
    json.JSONDecodeError or pydantic.ValidationError on a malformed/invalid
    plan - both caught by decision_validation.attempt_with_repair's caller
    below. An empty/missing content is NOT treated as repairable - see
    plan()'s handling of that case.
    """
    raw = json.loads(content)
    return AgentPlan.model_validate(raw)


def _log_planning_outcome(started: float, *, repaired: bool, outcome: str, failure_category: str | None) -> None:
    """Structured log line for one planning call. Fields only - never the
    user's message, the model's raw response, or any step argument value.
    """
    logger.info(
        "ollama planning provider call",
        extra={
            "provider": "ollama",
            "model": OLLAMA_MODEL,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "repaired": repaired,
            "outcome": outcome,
            "validation_failure_category": failure_category,
        },
    )


def plan(message: str) -> AgentPlan:
    """Ask a local Ollama model to produce a multi-step plan for the message.

    On a malformed/invalid first response, makes exactly one constrained
    repair attempt (re-asking with the error described) before giving up -
    never more than one, and never for a network/timeout failure, which
    goes straight to agent_planner.decide_plan's "planning failed" handling
    instead. Raises OllamaPlanningError if the request fails, times out,
    returns no content, or the JSON still doesn't validate into a
    well-formed AgentPlan after the repair attempt. The caller is expected
    to treat this as "planning failed", never as a cue to fall back to
    single-step execution of the original message - this function itself
    has no opinion on that, it only ever raises or returns a validated plan.
    """
    started = time.monotonic()
    initial_messages = [
        {"role": "system", "content": _PLANNING_SYSTEM_PROMPT},
        {"role": "user", "content": message},
    ]

    try:
        data = _call_ollama_plan(_build_payload(initial_messages))
    except Exception as exc:  # broad on purpose: a network/timeout failure never gets a repair attempt
        category = decision_validation.classify_validation_failure(exc)
        _log_planning_outcome(started, repaired=False, outcome="failed", failure_category=category)
        raise OllamaPlanningError(f"Ollama planning request failed: {exc}", category=category) from exc

    content = (data.get("message") or {}).get("content")
    if not content:
        # Not treated as repairable: no content at all isn't a shape the
        # repair prompt (which assumes a specific prior JSON attempt to
        # correct) can meaningfully address.
        _log_planning_outcome(started, repaired=False, outcome="failed", failure_category="empty_response")
        raise OllamaPlanningError("Ollama did not return a plan for this message.", category="empty_response")

    repair_attempted = []  # mutable cell _repair sets to non-empty as soon as it's called, success or not

    def _repair(exc: Exception) -> AgentPlan:
        repair_attempted.append(True)
        repair_messages = initial_messages + [
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": f"That plan was invalid: {exc}. Return a corrected plan in the same JSON schema.",
            },
        ]
        repair_data = _call_ollama_plan(_build_payload(repair_messages))
        repair_content = (repair_data.get("message") or {}).get("content")
        if not repair_content:
            raise OllamaPlanningError("Ollama did not return a plan for the repair attempt.", category="empty_response")
        return _parse_plan(repair_content)

    try:
        result = decision_validation.attempt_with_repair(primary=lambda: _parse_plan(content), repair=_repair)
    except Exception as exc:
        category = decision_validation.classify_validation_failure(exc)
        _log_planning_outcome(started, repaired=bool(repair_attempted), outcome="failed", failure_category=category)
        if isinstance(exc, OllamaPlanningError):
            raise
        raise OllamaPlanningError(str(exc), category=category) from exc

    repaired = bool(repair_attempted)
    _log_planning_outcome(
        started,
        repaired=repaired,
        outcome="planning_succeeded_after_repair" if repaired else "planning_succeeded_first_try",
        failure_category=None,
    )
    return result
