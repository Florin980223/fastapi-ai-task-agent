"""Decision provider dispatch for the agent, plus the rule-based provider.

decide_tool() below is the single entry point routes/agent.py calls. It
picks between two providers based on app.config.DECISION_PROVIDER:
- "rule_based" (default): _decide_tool_rule_based, defined in this file.
  Just checks the message text for a few keywords, in order, and picks
  the first tool whose keywords match, then extracts arguments with a
  few simple patterns. No AI/LLM involved.
- "anthropic": asks Claude to pick a tool (see anthropic_decision_provider.py).
  If that fails for any reason, this module logs a warning and falls
  back to the rule-based provider, so the endpoints always get an answer.
"""

import logging
import re

from app.config import DECISION_PROVIDER
from app.services import anthropic_decision_provider
from app.services.tool_decision import ToolDecision

logger = logging.getLogger(__name__)


# Each rule is (tool_name, keywords, reason). Rules are checked in
# order, and the first one whose keyword appears in the message wins.
#
# delete_task is checked before create_task on purpose: create_task's
# "todo" keyword would otherwise also match messages like "Delete todo
# 3" (which contains the word "todo"), wrongly creating a task instead
# of deleting one. Putting the more specific "delete"/"remove task"
# keywords first means a message like that is claimed by delete_task
# before create_task ever gets a chance to look at it.
RULES: list[tuple[str, list[str], str]] = [
    (
        "get_weather",
        ["weather", "temperature", "forecast", "city weather"],
        "The user is asking about weather.",
    ),
    (
        "delete_task",
        ["delete", "remove task"],
        "The user wants to delete a task.",
    ),
    (
        "create_task",
        ["create", "add", "new task", "todo"],
        "The user wants to create a new task.",
    ),
    (
        "list_tasks",
        ["list", "show tasks", "all tasks", "completed tasks", "unfinished tasks"],
        "The user wants to see a list of tasks.",
    ),
    (
        "update_task",
        ["update", "edit", "change task"],
        "The user wants to update an existing task.",
    ),
    (
        "mark_task_done",
        ["done", "complete", "finish task", "mark done"],
        "The user wants to mark a task as completed.",
    ),
]


def _select_tool(message: str) -> tuple[str | None, str]:
    """Pick a tool for the given message based on simple keyword rules.

    Returns a (selected_tool, reason) tuple. selected_tool is None if
    no rule matched.
    """
    lowered_message = message.lower()

    for tool_name, keywords, reason in RULES:
        if any(keyword in lowered_message for keyword in keywords):
            return tool_name, reason

    return None, "No matching tool was found for this message."


# Filler phrases to strip when turning a create_task message into a
# task title, e.g. "Add a task to buy milk" -> "buy milk".
_TASK_TITLE_FILLER_PHRASES = [
    "add a task to",
    "create a task to",
    "new task to",
    "todo",
]


def _extract_task_title(message: str) -> str:
    """Pull a short title out of a create_task message.

    Removes known filler phrases (case-insensitive). If nothing
    meaningful is left afterwards, falls back to the full message.
    """
    title = message
    lowered = message.lower()

    for phrase in _TASK_TITLE_FILLER_PHRASES:
        index = lowered.find(phrase)
        if index != -1:
            title = title[:index] + title[index + len(phrase):]
            lowered = title.lower()

    title = title.strip(" .!?")

    # Extraction was too weak (e.g. the whole message was filler words) -
    # just use the original message as-is.
    if not title:
        return message

    return title


def _extract_done_filter(message: str) -> bool | None:
    """Figure out which tasks a list_tasks message is asking for.

    Returns True for "completed"/"done", False for "unfinished" /
    "incomplete" / "not done", or None for "all tasks".
    """
    lowered = message.lower()

    if "unfinished" in lowered or "incomplete" in lowered or "not done" in lowered:
        return False
    if "completed" in lowered or "done" in lowered:
        return True
    return None


def _extract_city(message: str) -> str | None:
    """Pull a city name out of a weather message.

    Looks for patterns like "weather in London" or "forecast for
    Paris" by taking whatever comes after the last " in " / " for ".
    Returns None if no city could be found.
    """
    cleaned = message.strip().rstrip("?.!")
    lowered = cleaned.lower()

    for keyword in [" in ", " for "]:
        index = lowered.rfind(keyword)
        if index != -1:
            city = cleaned[index + len(keyword):].strip()
            if city:
                return city

    return None


def _extract_task_id(message: str) -> int | None:
    """Pull a task id out of a message, e.g. "Mark task 1 as done" -> 1.

    Returns the first run of digits found in the message, or None if
    there are no digits at all.
    """
    match = re.search(r"\d+", message)
    if match is None:
        return None
    return int(match.group())


def _extract_new_title(message: str) -> str | None:
    """Pull the new title out of an update_task message.

    Looks for "<task id> to <new title>", e.g. "Update task 1 to Call
    Alex tomorrow" -> "Call Alex tomorrow". Anchoring on the digits
    right before "to" (instead of just the first "to" in the message)
    means a title that itself contains "to" (e.g. "task 1 to Talk to
    Bob") is still captured in full. Returns None if no title found.
    """
    match = re.search(r"\d+\s+to\s+(.+)", message, re.IGNORECASE)
    if match is None:
        return None

    title = match.group(1).strip(" .!?")
    if not title:
        return None

    return title


def _build_arguments(selected_tool: str | None, message: str) -> dict[str, str | int | bool | None]:
    """Extract whatever arguments the chosen tool needs from the message."""
    if selected_tool == "create_task":
        return {"title": _extract_task_title(message)}

    if selected_tool == "list_tasks":
        return {"done": _extract_done_filter(message)}

    if selected_tool == "get_weather":
        return {"city": _extract_city(message)}

    if selected_tool == "mark_task_done":
        return {"task_id": _extract_task_id(message)}

    if selected_tool == "update_task":
        return {"task_id": _extract_task_id(message), "title": _extract_new_title(message)}

    if selected_tool == "delete_task":
        return {"task_id": _extract_task_id(message)}

    return {}


def _decide_tool_rule_based(message: str) -> ToolDecision:
    """Decide which tool (if any) a message should use, and with what arguments."""
    selected_tool, reason = _select_tool(message)
    arguments = _build_arguments(selected_tool, message)
    return ToolDecision(selected_tool=selected_tool, arguments=arguments, reason=reason)


def decide_tool(message: str) -> ToolDecision:
    """Decide which tool a message should use - the single entry point routes call.

    Uses the Anthropic provider when configured, falling back to the
    rule-based provider (the default) if it's not configured or if it
    fails for any reason.
    """
    if DECISION_PROVIDER == "anthropic":
        try:
            return anthropic_decision_provider.decide_tool(message)
        except anthropic_decision_provider.AnthropicDecisionError as exc:
            logger.warning("Anthropic decision provider failed (%s); falling back to rule-based.", exc)

    return _decide_tool_rule_based(message)
