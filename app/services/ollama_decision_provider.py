"""A local-Ollama-powered "decision provider" for the agent.

Like the Anthropic provider, this only decides WHICH tool to use and
WHAT arguments to call it with - it returns a ToolDecision and never
runs anything itself. Ollama is only ever asked to select one of the
app's own tools via its native (OpenAI-compatible) tool calling; it
cannot run arbitrary code, shell commands, or HTTP requests. The
caller (agent_decision.decide_tool) is responsible for falling back to
the rule-based provider if anything here fails.
"""

import copy
import json
import logging
import time

import httpx

from app.config import OLLAMA_BASE_URL, OLLAMA_MODEL, OLLAMA_TIMEOUT_SECONDS
from app.services import decision_validation, tool_schemas
from app.services.tool_decision import ToolDecision
from app.services.tool_registry import AVAILABLE_TOOLS

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are the decision-making layer of a small task-management assistant.\n\n"
    "Given the user's message, select the ONE tool that matches the user's "
    "final requested action - not an intermediate or preparatory step.\n\n"
    "Rules:\n"
    "- Always select the tool for what the user ultimately wants to happen, "
    "even if you don't have all the information needed to call it yet. "
    "Missing required arguments (like a task ID) are allowed and expected - "
    "do not avoid selecting a tool just because an argument is missing.\n"
    "- Do not select list_tasks merely because you don't know a task's ID. "
    "list_tasks is only for when the user explicitly wants to view, show, "
    "list, or filter their tasks - never as a way to \"look up\" an ID for "
    "another action.\n"
    "- Select delete_task when the user wants to delete or remove a task, "
    "even if they don't specify which one.\n"
    "- Select update_task when the user wants to rename, edit, or otherwise "
    "modify a task, even if the task ID or new title is missing.\n"
    "- Select mark_task_done when the user says a task is finished, done, or "
    "completed, even if they don't specify which one.\n"
    "- If no tool matches the user's message at all, don't call any tool.\n\n"
    "- When the user refers to a task by its name or a short description "
    "instead of a numeric id, call the tool with task_title set to that "
    "description instead of guessing or inventing a numeric task_id.\n\n"
    "Examples:\n"
    '- "I want to delete one of my tasks, but I do not know its ID" -> call delete_task with no arguments.\n'
    '- "I finished one of my tasks" -> call mark_task_done with no arguments.\n'
    '- "Show me my tasks" -> call list_tasks.\n'
    '- "Rename one of my tasks" -> call update_task with no arguments.\n'
    '- "Mark the presentation task as done" -> call mark_task_done with task_title="presentation".\n\n'
    "Respond only through the provided tool-call mechanism - never with plain prose."
)

# Local Ollama can be slow to respond on a cold model load - see
# app.config.OLLAMA_TIMEOUT_SECONDS for the actual (generous, more so
# than the Anthropic provider's) timeout value used below.


class OllamaDecisionError(decision_validation.DecisionProviderError):
    """Raised whenever the Ollama provider can't produce a trustworthy decision.

    Covers the server being unreachable/slow, invalid JSON, missing tool
    calls, unknown tools, and invalid arguments - agent_decision.decide_tool
    catches this one type and falls back to the rule-based provider.
    """


def _build_ollama_tools() -> list[dict]:
    """Build Ollama's native (OpenAI-style) tool definitions from the
    existing tool registry.

    Only tools with an argument schema are included, which limits Ollama
    to exactly the 6 executable tools (e.g. get_task is excluded - it
    has no schema, since this agent doesn't execute it).

    Each tool gets its own copy of the canonical schema with the JSON
    Schema "required" field dropped: a JSON Schema saying an argument is
    mandatory can make a local model avoid the tool entirely, or invent
    a value, when it's missing that argument - which directly conflicts
    with the system prompt's instruction that missing arguments are
    allowed. What's actually required is still defined by
    tool_schemas.REQUIRED_ARGUMENTS and enforced in Python afterwards
    (validate_tool_call for types, clarification.py for presence) -
    never by constraining the model's own schema. The canonical schema
    in tool_schemas.py (used by the Anthropic provider) is untouched;
    properties are deep-copied here so nothing this function does can
    ever mutate it.
    """
    tools = []
    for tool in AVAILABLE_TOOLS:
        if tool.name not in tool_schemas.TOOL_ARGUMENT_SCHEMAS:
            continue
        canonical_schema = tool_schemas.TOOL_ARGUMENT_SCHEMAS[tool.name]
        ollama_schema = {
            "type": canonical_schema["type"],
            "properties": copy.deepcopy(canonical_schema["properties"]),
        }
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": ollama_schema,
                },
            }
        )
    return tools


def _call_ollama(payload: dict) -> dict:
    """POST to Ollama's /api/chat and return the parsed JSON response.

    Kept as its own function so tests can monkeypatch it instead of
    making a real HTTP call.
    """
    response = httpx.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload, timeout=OLLAMA_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def _build_payload(messages: list[dict]) -> dict:
    return {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "tools": _build_ollama_tools(),
        "stream": False,
        "think": False,
        "options": {"temperature": 0},
    }


def _parse_tool_call(tool_calls: list[dict]) -> tuple[str | None, dict]:
    """Extract and validate a tool name/arguments from Ollama's tool_calls
    list. Only ever looks at the first call, even if the model returned
    more than one. Raises json.JSONDecodeError or
    tool_schemas.ToolCallValidationError on a malformed/invalid call -
    both are caught by decision_validation.attempt_with_repair's caller
    below.
    """
    function = tool_calls[0].get("function") or {}
    tool_name = function.get("name")
    arguments = function.get("arguments")

    # Ollama's arguments are usually already a JSON object, but some
    # models return them as a JSON string - handle that case too.
    if isinstance(arguments, str):
        arguments = json.loads(arguments)

    tool_schemas.validate_tool_call(tool_name, arguments)
    return tool_name, arguments


def _log_decision_outcome(
    started: float, *, repaired: bool, outcome: str, failure_category: str | None
) -> None:
    """Structured log line for one decision call. Fields only - never the
    user's message, the model's raw response, or any argument value.
    """
    logger.info(
        "ollama decision provider call",
        extra={
            "provider": "ollama",
            "model": OLLAMA_MODEL,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "repaired": repaired,
            "outcome": outcome,
            "validation_failure_category": failure_category,
        },
    )


def decide_tool(message: str) -> ToolDecision:
    """Ask a local Ollama model to pick a tool (and arguments) for the message.

    On a malformed/invalid first response, makes exactly one constrained
    repair attempt (re-asking with the error described) before giving up -
    never more than one, and never for a network/timeout failure, which
    goes straight to the caller's fallback-safety gate instead. Raises
    OllamaDecisionError if the request fails, times out, doesn't call a
    tool, or the tool call is still invalid after the repair attempt - the
    caller (agent_decision.decide_tool) decides what to do next, it is
    never assumed safe to just fall back to rule_based here.
    """
    started = time.monotonic()
    initial_messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": message},
    ]

    try:
        data = _call_ollama(_build_payload(initial_messages))
    except Exception as exc:  # broad on purpose: a network/timeout failure never gets a repair attempt
        category = decision_validation.classify_validation_failure(exc)
        _log_decision_outcome(started, repaired=False, outcome="failed", failure_category=category)
        raise OllamaDecisionError(f"Ollama request failed: {exc}", category=category) from exc

    tool_calls = (data.get("message") or {}).get("tool_calls") or []
    if not tool_calls:
        # Not treated as repairable: a model that declined to call any
        # tool may simply be correctly reporting "nothing here fits" -
        # forcing a repair attempt could push it into inventing a tool
        # call rather than confirming its own answer.
        _log_decision_outcome(started, repaired=False, outcome="failed", failure_category="empty_response")
        raise OllamaDecisionError("Ollama did not call a tool for this message.", category="empty_response")

    repair_attempted = []  # mutable cell _repair can set - True as soon as it's called, success or not

    def _repair(exc: Exception) -> tuple[str | None, dict]:
        repair_attempted.append(True)
        repair_messages = initial_messages + [
            {"role": "assistant", "content": "", "tool_calls": tool_calls},
            {
                "role": "user",
                "content": f"That tool call was invalid: {exc}. Call exactly one valid tool with corrected arguments.",
            },
        ]
        repair_data = _call_ollama(_build_payload(repair_messages))
        repair_tool_calls = (repair_data.get("message") or {}).get("tool_calls") or []
        if not repair_tool_calls:
            raise OllamaDecisionError("Ollama did not call a tool for the repair attempt.", category="empty_response")
        return _parse_tool_call(repair_tool_calls)

    try:
        tool_name, arguments = decision_validation.attempt_with_repair(
            primary=lambda: _parse_tool_call(tool_calls),
            repair=_repair,
        )
    except Exception as exc:
        category = decision_validation.classify_validation_failure(exc)
        _log_decision_outcome(started, repaired=bool(repair_attempted), outcome="failed", failure_category=category)
        if isinstance(exc, OllamaDecisionError):
            raise
        raise OllamaDecisionError(str(exc), category=category) from exc

    repaired = bool(repair_attempted)
    _log_decision_outcome(
        started,
        repaired=repaired,
        outcome="executed_after_repair" if repaired else "executed_first_try",
        failure_category=None,
    )
    return ToolDecision(
        selected_tool=tool_name,
        arguments=arguments,
        reason=f"Ollama (model {OLLAMA_MODEL}) selected the '{tool_name}' tool for this message.",
    )
