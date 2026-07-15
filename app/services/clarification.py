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


# Tools whose execution is irreversible enough to require the user to
# explicitly say yes before it runs, instead of executing as soon as the
# decision is complete.
_DESTRUCTIVE_TOOLS = {"delete_task"}


def requires_confirmation(selected_tool: str | None) -> bool:
    """Whether a complete decision for this tool must be confirmed before it runs."""
    return selected_tool in _DESTRUCTIVE_TOOLS


def build_confirmation_question(decision: ToolDecision) -> str:
    """Deterministic, tool-specific yes/no question for a destructive decision."""
    if decision.selected_tool == "delete_task":
        return f"Are you sure you want to delete task #{decision.arguments['task_id']}?"

    return "Are you sure you want to proceed?"


_CONFIRMATION_PHRASES = {"yes", "confirm", "proceed", "da"}
_CONFIRMATION_CANCELLATION_PHRASES = {"no", "cancel", "never mind", "stop", "nu"}


def is_confirmation_reply(message: str) -> bool:
    """Whether a follow-up reply confirms a pending destructive action."""
    normalized = message.strip().lower().strip(" .!?")
    return normalized in _CONFIRMATION_PHRASES


def is_confirmation_cancellation(message: str) -> bool:
    """Whether a follow-up reply cancels a pending destructive action."""
    normalized = message.strip().lower().strip(" .!?")
    return normalized in _CONFIRMATION_CANCELLATION_PHRASES


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


# Explicit, deterministic cues that a message refers back to a task
# mentioned earlier in the conversation, rather than being a generic,
# standalone request. Covers both the "fresh request" case ("Delete
# that task") and the "reply to a pending clarification" case ("that
# one"). Deliberately does NOT include bare words like "task" alone -
# only pronoun-like references count.
_REFERENCE_PHRASES = {
    "it",
    "that task",
    "this task",
    "the previous task",
    "the last task",
    "that one",
    "the previous one",
    "șterge-l",
    "marchează-l",
    "acel task",
    "acest task",
    "ultimul task",
    "acela",
    "ultimul",
}


def mentions_contextual_reference(message: str) -> bool:
    """Whether the message contains an explicit cue that it refers back
    to a task from earlier in the conversation (e.g. "it", "that task",
    "acela"), rather than being a generic/standalone request.

    Matching is case-insensitive and word-boundary aware, so short
    phrases like "it" only match as a standalone reference - not as
    part of another word (e.g. "edit", "item").
    """
    lowered = message.lower()
    return any(re.search(r"\b" + re.escape(phrase) + r"\b", lowered) for phrase in _REFERENCE_PHRASES)


def resolve_remembered_task_id(decision: ToolDecision, last_task_id: int | None, message: str) -> None:
    """Fill in a missing task_id from remembered conversation context -
    but only when the message explicitly refers back to a prior task.

    Never overwrites a task_id the provider or a parsed reply already
    supplied (that always wins), and never fires for a generic
    incomplete request ("Delete a task") just because context happens
    to be available - the message must contain an explicit reference
    cue (see mentions_contextual_reference). This keeps a bare
    incomplete request asking for clarification instead of silently
    acting on a guessed, possibly-destructive target.
    """
    if last_task_id is None or decision.selected_tool is None:
        return

    if "task_id" not in tool_schemas.REQUIRED_ARGUMENTS.get(decision.selected_tool, {}):
        return

    if decision.arguments.get("task_id") is not None:
        return

    if not mentions_contextual_reference(message):
        return

    decision.arguments["task_id"] = last_task_id


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
