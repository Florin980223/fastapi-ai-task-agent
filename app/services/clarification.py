"""Deterministic clarification-question generation for incomplete decisions.

If a decision provider (rule-based, Anthropic, or Ollama) identified the
right tool but a required argument is missing, POST /agent/execute must
not execute the tool - it should ask a specific question instead. This
module decides whether that's the case and, if so, what to ask.

It's also responsible for handling the follow-up reply to that question
(e.g. "3") - recognizing cancellation, and deterministically merging
whatever the reply supplies into the pending decision. Follow-up replies
are never sent to an LLM provider; only plain string/regex parsing.

Required arguments come from tool_schemas.REQUIRED_ARGUMENTS - the same
source of truth every provider's own validation already uses - so
there's exactly one place that defines what's "required" for a tool.
No LLM is involved anywhere in this file.
"""

import re

from app.services import tool_schemas
from app.services.conversation_memory import PendingClarification
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


_CANCELLATION_PHRASES = {"cancel", "never mind", "stop"}


def is_cancellation(message: str) -> bool:
    """Whether a follow-up reply cancels the pending clarification."""
    normalized = message.strip().lower().strip(" .!?")
    return normalized in _CANCELLATION_PHRASES


def _extract_integer_from_reply(message: str) -> int | None:
    """Pull the first run of digits out of a short follow-up reply, e.g.
    "3" or "task 3" -> 3. Same idea as agent_decision._extract_task_id,
    kept as a small local copy rather than importing across a
    module-privacy boundary for one three-line regex.
    """
    match = re.search(r"\d+", message)
    if match is None:
        return None
    return int(match.group())


def merge_reply(pending: PendingClarification, message: str) -> dict[str, str | int | bool | None]:
    """Deterministically parse a follow-up reply and merge whatever it
    supplies into the pending decision's arguments. Never calls an LLM.

    Only fills in arguments that are still missing; anything the reply
    doesn't clearly answer is left as-is for another round.
    """
    merged = dict(pending.arguments)
    missing = set(pending.missing)
    tool = pending.selected_tool

    if tool in {"mark_task_done", "delete_task"} and "task_id" in missing:
        task_id = _extract_integer_from_reply(message)
        if task_id is not None:
            merged["task_id"] = task_id

    elif tool == "create_task" and "title" in missing:
        title = message.strip()
        if title:
            merged["title"] = title

    elif tool == "get_weather" and "city" in missing:
        city = message.strip()
        if city:
            merged["city"] = city

    elif tool == "update_task":
        if missing == {"title"}:
            title = message.strip()
            if title:
                merged["title"] = title
        elif missing == {"task_id"}:
            task_id = _extract_integer_from_reply(message)
            if task_id is not None:
                merged["task_id"] = task_id
        elif missing == {"task_id", "title"}:
            task_id = _extract_integer_from_reply(message)
            if task_id is not None:
                merged["task_id"] = task_id
                # Use whatever's left after removing the id as the title,
                # e.g. "3 Buy groceries" -> title "Buy groceries".
                remainder = re.sub(r"\d+", "", message, count=1).strip(" ,.-")
                if remainder:
                    merged["title"] = remainder
            else:
                # No digit at all - treat the whole reply as the title
                # instead, and keep waiting for the task id.
                title = message.strip()
                if title:
                    merged["title"] = title

    return merged
