"""Decision provider dispatch for the agent, plus the rule-based provider.

decide_tool() below is the single entry point routes/agent.py calls. It
picks between two providers based on app.config.DECISION_PROVIDER:
- "rule_based" (default): _decide_tool_rule_based, defined in this file.
  Just checks the message text for a few keywords, in order, and picks
  the first tool whose keywords match, then extracts arguments with a
  few simple patterns. No AI/LLM involved.
- "anthropic": asks Claude to pick a tool (see anthropic_decision_provider.py).
- "ollama": asks a local Ollama model to pick a tool (see ollama_decision_provider.py).
  If either LLM provider fails for any reason, this module logs a warning
  and falls back to the rule-based provider, so the endpoints always get
  an answer.
"""

import logging
import re

from app.config import DECISION_PROVIDER
from app.services import anthropic_decision_provider, clarification, decision_validation, ollama_decision_provider
from app.services.tool_decision import ToolDecision

logger = logging.getLogger(__name__)


class UnsafeFallbackError(Exception):
    """Raised by decide_tool when a configured LLM provider failed and
    falling back to the deterministic rule_based provider would not be
    safe for this particular message (see _safe_to_fall_back).

    The caller (routes/agent.py) must produce a clean, no-execution
    response - never retry, never guess, never execute anything. This is
    never raised for rule_based itself, which is never wrapped or gated by
    any of this - only a configured Anthropic/Ollama failure can reach it.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
        ["update", "edit", "change task", "rename"],
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


# Bare, content-free ways of asking to create a task, with no actual
# title in them. If stripping filler phrases leaves one of these
# untouched, there's no real title to extract - the caller should treat
# it as missing (see clarification.py) rather than using it verbatim.
_BARE_CREATE_TASK_PHRASES = {
    "create a task",
    "create task",
    "add a task",
    "add task",
    "new task",
    "todo",
}


def _extract_task_title(message: str) -> str | None:
    """Pull a short title out of a create_task message.

    Removes known filler phrases (case-insensitive). Returns None if
    nothing but a bare, content-free command is left afterwards.
    """
    title = message
    lowered = message.lower()

    for phrase in _TASK_TITLE_FILLER_PHRASES:
        index = lowered.find(phrase)
        if index != -1:
            title = title[:index] + title[index + len(phrase):]
            lowered = title.lower()

    title = title.strip(" .!?")

    if not title or title.lower() in _BARE_CREATE_TASK_PHRASES:
        return None

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


# Filler phrases stripped (in order) when turning a mark_task_done/
# delete_task/update_task message into a free-text task reference, e.g.
# "Mark the client presentation task as done" -> "client presentation".
# Only ever used when _extract_task_id found no digit (see _build_arguments)
# - a digit-containing message is unaffected by any of this. Same
# find-and-remove-each-phrase-in-turn idiom as _extract_task_title's
# _TASK_TITLE_FILLER_PHRASES; "the <verb>" combos are listed before their
# bare counterpart so a message with "the" is fully consumed in one pass
# rather than leaving a stray "the" behind.
_TASK_REFERENCE_FILLER_PHRASES = [
    "mark the",
    "mark",
    "complete the",
    "completed",
    "complete",
    "finish the",
    "finished",
    "finish",
    "delete the",
    "delete",
    "remove the",
    "remove",
    "update the",
    "update",
    "edit the",
    "edit",
    "rename the",
    "rename",
    "change the",
    "change",
    "as done",
    "as complete",
    "as completed",
    "the task",
    "task",
]

# Bare articles, dropped as whole words (never via substring removal,
# unlike the multi-word phrases above) after filler-phrase stripping, so
# a message with no real content beyond filler - e.g. "Delete a task",
# "Mark a task as done" - collapses to no reference at all instead of a
# stray "a". Word-level comparison specifically avoids the substring bug
# a bare "a" filler phrase would have: "a" is itself a substring of
# "task", so a naive .find("a") removal would corrupt "task" into "tsk".
_REFERENCE_STOPWORDS = {"a", "an", "the"}


def _extract_task_reference(message: str) -> str | None:
    """Pull a free-text reference to an existing task out of a message
    that has no numeric task id, e.g. "Mark the client presentation task
    as done" -> "client presentation", or "Delete the old testing task"
    -> "old testing".

    This is deliberately a best-effort heuristic, not a precise parser:
    app.services.task_resolution's tiered (exact / containment / fuzzy)
    matching is forgiving of a slightly over- or under-stripped
    reference, so perfect extraction here isn't required for correct
    resolution. Returns None if nothing but filler is left afterwards.
    """
    reference = message
    lowered = message.lower()

    for phrase in _TASK_REFERENCE_FILLER_PHRASES:
        index = lowered.find(phrase)
        if index != -1:
            reference = reference[:index] + reference[index + len(phrase):]
            lowered = reference.lower()

    reference = reference.strip(" .!?")
    words = [word for word in reference.split() if word.lower() not in _REFERENCE_STOPWORDS]
    return " ".join(words) or None


def _extract_update_reference_and_title(message: str) -> tuple[str | None, str | None]:
    """Split a digit-free update_task message into (task reference, new
    title), e.g. "Rename the portfolio task to Prepare final portfolio"
    -> ("portfolio", "Prepare final portfolio").

    Anchors on the FIRST " to " in the message (case-insensitive) and
    takes everything after it as the new title - same convention as the
    existing digit-anchored _extract_new_title's digit-then-"to" regex
    (anchor at the earliest separator, then greedily capture the
    rest), so a new title that itself contains "to" (e.g. "Update the
    drive task to Talk to Bob" -> new title "Talk to Bob") is still
    captured in full. Only called when _extract_task_id found no digit -
    the existing digit-anchored path is tried first and is unaffected by
    any of this (see _build_arguments). Returns (None, None) if the
    message has no " to " at all.
    """
    lowered = message.lower()
    index = lowered.find(" to ")
    if index == -1:
        return None, None

    reference = _extract_task_reference(message[:index])
    new_title = message[index + len(" to "):].strip(" .!?") or None
    return reference, new_title


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

    if selected_tool in ("mark_task_done", "delete_task"):
        task_id = _extract_task_id(message)
        if task_id is not None:
            # Digit present - unchanged, byte-identical behavior. No
            # task_title extraction is even attempted: it would be
            # wasted work, since app.services.task_resolution never
            # looks at task_title once task_id is present.
            return {"task_id": task_id}
        return {"task_id": None, "task_title": _extract_task_reference(message)}

    if selected_tool == "update_task":
        task_id = _extract_task_id(message)
        if task_id is not None:
            return {"task_id": task_id, "title": _extract_new_title(message)}
        task_title, new_title = _extract_update_reference_and_title(message)
        return {"task_id": None, "task_title": task_title, "title": new_title}

    return {}


def _decide_tool_rule_based(message: str) -> ToolDecision:
    """Decide which tool (if any) a message should use, and with what arguments."""
    selected_tool, reason = _select_tool(message)
    arguments = _build_arguments(selected_tool, message)
    return ToolDecision(selected_tool=selected_tool, arguments=arguments, reason=reason)


def _count_matching_rules(message: str) -> int:
    """How many of RULES' own keyword triggers match this message.

    Used only by _safe_to_fall_back to judge whether rule_based itself
    would be confident/unambiguous about this message - never to select a
    tool (that's still _select_tool's first-match-wins job, unchanged).
    """
    lowered = message.lower()
    return sum(1 for _, keywords, _ in RULES if any(keyword in lowered for keyword in keywords))


# Failure categories (see decision_validation.classify_validation_failure)
# where the model picked a real tool but supplied bad arguments - as
# opposed to a totally garbled/unknown-tool response. Re-deriving
# arguments from raw text via rule_based's crude regex extraction on a
# message we already know produced bad arguments once is exactly the kind
# of guess this gate rules out.
_ARGUMENT_FAILURE_CATEGORIES = {"wrong_type", "unknown_argument"}


def _safe_to_fall_back(message: str, failure_category: str | None) -> bool:
    """Whether it's safe to fall back to the deterministic rule_based
    provider after a configured LLM provider has failed.

    Returns False - fail safely instead of guessing - when:
    - the message is multi-step-shaped (rule_based can only ever pick one
      tool, silently dropping the rest of a multi-action request);
    - the message contains destructive-intent phrasing;
    - the message contains a bare contextual reference ("it"/"that one");
    - the failure was specifically that the model picked a real tool but
      supplied bad arguments (_ARGUMENT_FAILURE_CATEGORIES);
    - the message is ambiguous by rule_based's own reckoning - it matches
      more than one of RULES' own keyword triggers, so rule_based can't
      cleanly agree with itself on which tool applies either.

    Returns True only otherwise: the request is single-step-shaped,
    non-destructive, has no contextual reference, and the failure (if any)
    wasn't an argument-validation failure. In that narrow case rule_based's
    own extraction never invents a value - it only ever pulls from the
    literal message text - so "preserve the same user intent without
    inventing information" holds by construction.
    """
    if clarification.looks_multi_step(message):
        return False
    if clarification.contains_destructive_intent(message):
        return False
    if clarification.mentions_contextual_reference(message):
        return False
    if failure_category in _ARGUMENT_FAILURE_CATEGORIES:
        return False
    if _count_matching_rules(message) > 1:
        return False
    return True


def _handle_provider_failure(provider_name: str, message: str, exc: decision_validation.DecisionProviderError) -> ToolDecision:
    """Called only when a configured LLM provider has already exhausted
    its one repair attempt (or failed immediately on a network/timeout
    error) and raised. Either falls back to rule_based (only when
    _safe_to_fall_back says so) or raises UnsafeFallbackError - never
    executes anything itself, never retries the provider again.
    """
    if _safe_to_fall_back(message, exc.category):
        logger.warning("%s decision provider failed (category=%s); falling back to rule-based.", provider_name, exc.category)
        logger.info(
            "decision fallback",
            extra={"provider": provider_name, "outcome": "fallback_to_rule_based", "validation_failure_category": exc.category},
        )
        return _decide_tool_rule_based(message)

    logger.warning("%s decision provider failed (category=%s); not safe to fall back to rule-based.", provider_name, exc.category)
    logger.info(
        "decision fallback",
        extra={"provider": provider_name, "outcome": "unsafe_fallback_blocked", "validation_failure_category": exc.category},
    )
    raise UnsafeFallbackError(
        "I couldn't safely process that request. Could you rephrase it or state exactly what you'd like me to do?"
    ) from exc


def decide_tool(message: str) -> ToolDecision:
    """Decide which tool a message should use - the single entry point routes call.

    Uses the Anthropic or Ollama provider when configured. On a provider
    failure (after its own one repair attempt), falls back to the
    rule-based provider only when that's judged safe for this particular
    message (see _safe_to_fall_back); otherwise raises UnsafeFallbackError
    instead of guessing. rule_based itself (the default when no LLM
    provider is configured) is never wrapped, retried, or gated by any of
    this - it always runs directly.
    """
    if DECISION_PROVIDER == "anthropic":
        try:
            return anthropic_decision_provider.decide_tool(message)
        except decision_validation.DecisionProviderError as exc:
            return _handle_provider_failure("anthropic", message, exc)
    elif DECISION_PROVIDER == "ollama":
        try:
            return ollama_decision_provider.decide_tool(message)
        except decision_validation.DecisionProviderError as exc:
            return _handle_provider_failure("ollama", message, exc)

    return _decide_tool_rule_based(message)
