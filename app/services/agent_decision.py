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
from typing import NamedTuple

from app.config import DECISION_PROVIDER
from app.services import anthropic_decision_provider, clarification, decision_validation, ollama_decision_provider, tool_schemas
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
        [
            "update",
            "edit",
            "change task",
            "change the",
            "rename",
            # Added for priority/due-date planning commands - see
            # _extract_priority/_extract_due_date. Multi-word phrases
            # ("set the", "move the") deliberately, not bare "set"/"move":
            # bare "set" is a substring of "asset"/"reset"/"offset", and
            # bare "move" is a substring of "removed" - "set the"/"move
            # the" avoid both false-positive classes while still matching
            # every required planning-command example.
            "make",
            "set the",
            "move the",
            "clear the deadline",
            "clear the due date",
            "remove the deadline",
            "remove the due date",
        ],
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
# task title, e.g. "Add a task to buy milk" -> "buy milk", or "Create a
# task called Review documentation" -> "Review documentation". The
# "called" variants are listed with their full verb prefix (mirroring
# the existing "X a task to" phrases) so nothing but the real title is
# ever left behind; "task called" alone is a fallback for any other
# verb this list doesn't enumerate.
_TASK_TITLE_FILLER_PHRASES = [
    "add a task to",
    "create a task to",
    "new task to",
    "add a task called",
    "create a task called",
    "new task called",
    "task called",
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


# An explicit task id must sit in an ID-shaped grammatical position: right
# after the word "task"/"todo" (optionally with a "#" and/or whitespace in
# between - "task 9", "task #9", "task#9", "todo 3"), AND immediately
# followed by an expected command boundary/connector - "as"/"to" (as their
# own word, via \b, so "assignment"/"total" never count), a punctuation
# mark, or the end of the message. Without that trailing boundary check, a
# title's own leading number - "task 2026 roadmap", "task 9abc" - would
# wrongly look like an id just because it happens to follow the word
# "task". Digits with no task/todo word before them (e.g. "Q3", "2026" in
# "Mark Project 2026 roadmap as done") never match at all, regardless of
# this trailing-boundary check - see the update to _build_arguments's
# docstring-adjacent tests for the exact scenarios this covers.
_TASK_ID_PATTERN = re.compile(r"\b(?:task|todo)\s*#?\s*(\d+)(?=\s+as\b|\s+to\b|[.,!?]|\s*$)", re.IGNORECASE)


def _extract_task_id(message: str) -> int | None:
    """Pull an explicit task id out of a message, e.g. "Mark task 1 as
    done" -> 1, "Delete task #12" -> 12, "Delete todo 3" -> 3.

    Only matches a run of digits that both follows "task"/"todo" and is
    followed by an expected command boundary (see _TASK_ID_PATTERN) - a
    number that's part of a title, e.g. "Mark task 2026 roadmap as done"
    or "Mark Q3 report as done", is never mistaken for an id. Returns None
    if no such id-shaped reference is found.
    """
    match = _TASK_ID_PATTERN.search(message)
    if match is None:
        return None
    return int(match.group(1))


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
    "make it",
    "make the",
    "make",
    "set the",
    "move the",
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
# "for"/"and" are here for the same reason, specifically to clean up
# connector residue left over once a matched priority/due-date phrase is
# stripped out of a combined command before reference-extraction runs -
# see _strip_span/_build_arguments's update_task branch - e.g. "Clear the
# deadline for the client contract task" -> (after stripping the due-date
# clear phrase) "for the client contract task" -> "client contract".
_REFERENCE_STOPWORDS = {"a", "an", "the", "for", "and"}


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


# --- Priority (create_task/update_task only) -------------------------------
#
# Deterministic and narrow by design: a canonical value or synonym is only
# ever recognized in a supported command position - immediately before the
# word "task" ("a high-priority task"), or trailing at the end of a clause
# ("make it high priority", "to medium priority"). Anything else (a bare
# synonym word anywhere, or the phrase mid-title followed by more title
# words) is left completely alone - never guessed, never normalized. This
# is what protects a genuine title like "High Priority Clients" or "Urgent
# Customer Review": "High Priority" there is followed by "Clients", which
# matches none of the trailing-boundary alternatives below, so it never
# matches at all.

_PRIORITY_SYNONYMS = {
    "low": "low",
    "medium": "medium",
    "normal": "medium",
    "high": "high",
    "urgent": "high",
    "important": "high",
}

_PRIORITY_PATTERN = re.compile(
    r"\b(low|medium|high|urgent|important|normal)[\s-]+priority\b(?=\s+task\b|\s+and\b|[.,!?]|\s*$)",
    re.IGNORECASE,
)


def _find_priority_match(message: str) -> re.Match | None:
    return _PRIORITY_PATTERN.search(message)


def _extract_priority(message: str) -> str | None:
    """Pull a canonical priority (low/medium/high) out of a message, e.g.
    "a high-priority task" or "make it high priority" -> "high". Returns
    None if no recognized priority phrase is present - never a guess.
    """
    match = _find_priority_match(message)
    if match is None:
        return None
    return _PRIORITY_SYNONYMS[match.group(1).lower()]


# --- Due date (create_task/update_task only) --------------------------------
#
# Deliberately narrow, deterministic "shapes" only - never a bare
# occurrence of "due"/"deadline" anywhere in the message (that would
# misfire on genuine titles like "Review deadline policy" or "Due
# diligence review" - see the module's tests). Each shape below anchors on
# a distinctive connector ("deadline to/on", "due date", or bare "due"
# immediately followed by a digit-shaped token) that a normal title
# sentence essentially never produces incidentally.


class _DueDateExtraction(NamedTuple):
    """Result of looking for a due-date reference in a message.

    mentioned: whether the message referenced a due date at all (clear
      phrase, or one of the recognized shapes) - False means "not
      mentioned", the caller must omit the due_date argument entirely
      (never write None), which is what preserves "omitted means
      unchanged" for update_task.
    value: a validated "YYYY-MM-DD" string, or None (either "not
      mentioned" or "explicit clear" - see `mentioned`/`unclear` to tell
      those apart).
    unclear: True when a due-date shape was matched but what followed
      wasn't a valid explicit calendar date (a relative phrase like "next
      Friday", or a malformed date like "2026-13-45") - the caller must
      ask for clarification, never guess or execute with this value.
    span: the (start, end) range that should be stripped from the message
      before running the existing task-id/reference/title extractors on
      it, so e.g. "due 2026-08-15" never leaks into a new task title.
      When unclear, this is widened to the end of the clause (next
      sentence-ending punctuation, or end of message) rather than just
      the connector + one token, so an ambiguous tail like "next Friday"
      is fully removed too - otherwise leftover words like "Friday" could
      corrupt task-title resolution before the clarification this
      triggers ever gets a chance to run (task_resolution runs first -
      see routes/agent.py). None when nothing matched at all.
    """

    mentioned: bool
    value: str | None
    unclear: bool
    span: tuple[int, int] | None = None


_DUE_DATE_CLEAR_PATTERN = re.compile(r"\b(?:clear|remove)\s+the\s+(?:deadline|due\s+date)\b", re.IGNORECASE)

# Checked in order; the first one whose connector appears wins. Each
# captures a single following token, used only to test whether it's a
# valid explicit "YYYY-MM-DD" date - never used verbatim if invalid (see
# _extract_due_date, which widens the stripped span when it isn't).
_DUE_DATE_SHAPE_PATTERNS = [
    re.compile(r"\bdeadline\s+to\s+(\S+)", re.IGNORECASE),
    re.compile(r"\bdeadline\s+on\s+(\S+)", re.IGNORECASE),
    re.compile(r"\bdue\s+date\s+(\S+)", re.IGNORECASE),
    # Bare "due" only counts when immediately followed by a digit-leading
    # token - this is what lets "Due diligence review" pass through
    # untouched (no digit follows "due" there) while still catching "due
    # 2026-08-15" and "due 2026-13-45" (malformed, but clearly an attempt).
    re.compile(r"\bdue\s+(\d\S*)", re.IGNORECASE),
]

_CLAUSE_END_PATTERN = re.compile(r"[.,!?]|$")


def _extract_due_date(message: str) -> _DueDateExtraction:
    clear_match = _DUE_DATE_CLEAR_PATTERN.search(message)
    if clear_match is not None:
        return _DueDateExtraction(mentioned=True, value=None, unclear=False, span=clear_match.span())

    for pattern in _DUE_DATE_SHAPE_PATTERNS:
        match = pattern.search(message)
        if match is None:
            continue
        token = match.group(1).strip(" .,!?")
        if tool_schemas.is_valid_iso_date(token):
            return _DueDateExtraction(mentioned=True, value=token, unclear=False, span=match.span())
        clause_end = match.start() + _CLAUSE_END_PATTERN.search(message[match.start():]).start()
        return _DueDateExtraction(mentioned=True, value=None, unclear=True, span=(match.start(), clause_end))

    return _DueDateExtraction(mentioned=False, value=None, unclear=False, span=None)


def _strip_planning_spans(message: str, spans: list[tuple[int, int] | None]) -> str:
    """Remove each (start, end) span from `message`, rightmost first (so
    earlier spans' positions - computed against the original message -
    stay valid throughout), then collapse the resulting whitespace gaps
    to single spaces. Only ever called when at least one span was
    actually found (see _build_arguments) - a message with no
    priority/due-date wording never reaches this function at all, so it
    is never touched or reformatted in any way.
    """
    real_spans = sorted((s for s in spans if s is not None), key=lambda s: s[0], reverse=True)
    cleaned = message
    for start, end in real_spans:
        cleaned = cleaned[:start] + cleaned[end:]
    return re.sub(r"\s+", " ", cleaned)


def _discard_bare_connector(text: str | None) -> str | None:
    """None if `text` is empty or nothing but the leftover "and" connector
    from stripping a combined priority/due-date phrase out of a message
    before title-extraction ran (see _build_arguments's update_task
    branch) - e.g. "...to high priority and due 2026-08-15" strips both
    planning phrases, leaving "and" where a new title would have been,
    which is never a legitimate title on its own.
    """
    if text is None:
        return None
    if text.strip(" .!?").lower() in ("", "and"):
        return None
    return text


def _build_arguments(
    selected_tool: str | None, message: str
) -> tuple[dict[str, str | int | bool | None], tuple[str, ...]]:
    """Extract whatever arguments the chosen tool needs from the message.

    Returns (arguments, needs_clarification_for) - the second element is
    only ever non-empty for create_task/update_task, and only ever
    contains "due_date" (see _extract_due_date) - everything else always
    returns an empty tuple.
    """
    if selected_tool == "create_task":
        priority_match = _find_priority_match(message)
        priority = _PRIORITY_SYNONYMS[priority_match.group(1).lower()] if priority_match else None
        due_date_result = _extract_due_date(message)

        spans = [priority_match.span() if priority_match else None, due_date_result.span]
        cleaned_message = _strip_planning_spans(message, spans) if any(spans) else message

        arguments: dict[str, str | int | bool | None] = {"title": _extract_task_title(cleaned_message)}
        if priority is not None:
            arguments["priority"] = priority
        if due_date_result.mentioned and not due_date_result.unclear:
            arguments["due_date"] = due_date_result.value

        needs_clarification = ("due_date",) if due_date_result.unclear else ()
        return arguments, needs_clarification

    if selected_tool == "list_tasks":
        return {"done": _extract_done_filter(message)}, ()

    if selected_tool == "get_weather":
        return {"city": _extract_city(message)}, ()

    if selected_tool in ("mark_task_done", "delete_task"):
        task_id = _extract_task_id(message)
        if task_id is not None:
            # Digit present - unchanged, byte-identical behavior. No
            # task_title extraction is even attempted: it would be
            # wasted work, since app.services.task_resolution never
            # looks at task_title once task_id is present.
            return {"task_id": task_id}, ()
        return {"task_id": None, "task_title": _extract_task_reference(message)}, ()

    if selected_tool == "update_task":
        priority_match = _find_priority_match(message)
        priority = _PRIORITY_SYNONYMS[priority_match.group(1).lower()] if priority_match else None
        due_date_result = _extract_due_date(message)
        needs_clarification = ("due_date",) if due_date_result.unclear else ()

        spans = [priority_match.span() if priority_match else None, due_date_result.span]
        cleaned_message = _strip_planning_spans(message, spans) if any(spans) else message

        task_id = _extract_task_id(cleaned_message)
        if task_id is not None:
            new_title = _discard_bare_connector(_extract_new_title(cleaned_message))
            arguments = {"task_id": task_id, "title": new_title}
        else:
            task_title, new_title = _extract_update_reference_and_title(cleaned_message)
            new_title = _discard_bare_connector(new_title)
            if task_title is None and new_title is None and (priority is not None or due_date_result.mentioned):
                # A priority/due-date mutation was found but there's no
                # " to <new title>" rename clause at all (e.g. "Make the
                # client contract task high priority") - still extract a
                # bare task reference the same way mark_task_done/
                # delete_task do. Deliberately gated on priority/due_date
                # being present: an ordinary update message with neither
                # keeps its existing, unchanged behavior of extracting no
                # reference at all without a " to " clause (see
                # test_update_task_with_no_to_separator_and_no_digit_has_none_arguments).
                task_title = _extract_task_reference(cleaned_message)
            arguments = {"task_id": None, "task_title": task_title, "title": new_title}

        if priority is not None:
            arguments["priority"] = priority
        if due_date_result.mentioned and not due_date_result.unclear:
            arguments["due_date"] = due_date_result.value

        return arguments, needs_clarification

    return {}, ()


def _decide_tool_rule_based(message: str) -> ToolDecision:
    """Decide which tool (if any) a message should use, and with what arguments."""
    selected_tool, reason = _select_tool(message)
    arguments, needs_clarification_for = _build_arguments(selected_tool, message)
    return ToolDecision(
        selected_tool=selected_tool,
        arguments=arguments,
        reason=reason,
        needs_clarification_for=needs_clarification_for,
    )


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
