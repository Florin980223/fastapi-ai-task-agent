"""Deterministic clarification-question generation for incomplete decisions.

If a decision provider (rule-based, Anthropic, or Ollama) identified the
right tool but a required argument is missing, POST /agent/execute must
not execute the tool - it should ask a specific question instead. This
module decides whether that's the case and, if so, what to ask.

Required arguments come from tool_schemas.REQUIRED_ARGUMENTS - the same
source of truth every provider's own validation already uses - so
there's exactly one place that defines what's "required" for a tool.
No LLM is involved anywhere in this file.
"""

from app.services import tool_schemas
from app.services.tool_decision import ToolDecision

# Deterministic phrasing for tools with exactly one required argument.
_SINGLE_ARGUMENT_QUESTIONS: dict[str, str] = {
    "create_task": "What should the task title be?",
    "get_weather": "Which city would you like the weather for?",
    "mark_task_done": "Which task ID should I mark as done?",
    "delete_task": "Which task ID should I delete?",
}

# update_task is the only tool with two required arguments, so its
# question depends on which one(s) are missing.
_UPDATE_TASK_QUESTIONS: dict[frozenset[str], str] = {
    frozenset({"task_id"}): "Which task ID would you like to update?",
    frozenset({"title"}): "What should the new title be?",
    frozenset({"task_id", "title"}): "Which task ID would you like to update, and what should the new title be?",
}


def missing_arguments(decision: ToolDecision) -> list[str]:
    """Return which required arguments for decision.selected_tool are missing.

    "Missing" means absent or explicitly None. Returns an empty list if
    no tool was selected at all (an unmatched message is never treated
    as a clarifiable tool call) or if the tool has no required arguments
    (e.g. list_tasks).
    """
    if decision.selected_tool is None:
        return []

    required = tool_schemas.REQUIRED_ARGUMENTS.get(decision.selected_tool, {})
    return [name for name in required if decision.arguments.get(name) is None]


def build_reason(selected_tool: str, missing: list[str]) -> str:
    """Deterministic, generic explanation of why clarification is needed."""
    return f"The {selected_tool} tool requires {', '.join(missing)}."


def build_clarification_question(selected_tool: str, missing: list[str]) -> str:
    """Deterministic, tool-specific question asking for the missing argument(s)."""
    if selected_tool == "update_task":
        return _UPDATE_TASK_QUESTIONS[frozenset(missing)]

    return _SINGLE_ARGUMENT_QUESTIONS[selected_tool]
