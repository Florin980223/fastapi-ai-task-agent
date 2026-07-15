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

import httpx
from pydantic import ValidationError

from app.config import OLLAMA_BASE_URL, OLLAMA_MODEL
from app.services.agent_plan import AgentPlan

_PLANNING_SYSTEM_PROMPT = (
    "You are the planning layer of a small task-management assistant.\n\n"
    "Given the user's message, break it down into an ordered plan of 2 or 3 "
    "steps, where each step calls exactly one of these tools:\n"
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
    "Only ever produce 2 or 3 steps. If the user's message doesn't clearly "
    "need more than one tool, or you cannot break it into a safe ordered "
    "plan using only the tools above, still return your best 2-3 step "
    "attempt using only those tools - the caller will validate it.\n\n"
    "Example - \"Create a task to buy milk and then show me all tasks\":\n"
    '{"steps": [{"tool": "create_task", "arguments": {"title": "buy milk"}}, '
    '{"tool": "list_tasks", "arguments": {}}]}\n\n'
    "Example - \"Add a task to call Alex and then mark it done\":\n"
    '{"steps": [{"tool": "create_task", "arguments": {"title": "call Alex"}}, '
    '{"tool": "mark_task_done", "arguments": {}, "task_id_from_step": 1}]}'
)

# Local Ollama can be slow to respond on a cold model load, same generous
# timeout as the single-step Ollama provider.
_REQUEST_TIMEOUT_SECONDS = 30.0


class OllamaPlanningError(Exception):
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
    response = httpx.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload, timeout=_REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def plan(message: str) -> AgentPlan:
    """Ask a local Ollama model to produce a multi-step plan for the message.

    Raises OllamaPlanningError if the request fails, times out, returns
    invalid JSON, or the JSON doesn't validate into a well-formed AgentPlan
    (including having the wrong number of steps). The caller is expected to
    treat this as "planning failed", not as a cue to fall back to
    single-step execution of the original message.
    """
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": _PLANNING_SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
        "stream": False,
        "think": False,
        # Ollama structured-output mode: constrains the model's JSON to
        # this schema. This is a best-effort hint only - agent_planner
        # fully re-validates every field afterward regardless.
        "format": AgentPlan.model_json_schema(),
        "options": {"temperature": 0},
    }

    try:
        data = _call_ollama_plan(payload)
    except Exception as exc:  # broad on purpose: any failure here must be treated as "planning failed"
        raise OllamaPlanningError(f"Ollama planning request failed: {exc}") from exc

    content = (data.get("message") or {}).get("content")
    if not content:
        raise OllamaPlanningError("Ollama did not return a plan for this message.")

    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        raise OllamaPlanningError(f"Ollama returned invalid JSON for a plan: {exc}") from exc

    try:
        return AgentPlan.model_validate(raw)
    except ValidationError as exc:
        raise OllamaPlanningError(f"Ollama returned an invalid plan shape: {exc}") from exc
