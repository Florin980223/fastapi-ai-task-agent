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
from app.services.task_resolution import TaskCandidate
from app.services.tool_decision import ToolDecision

# Private-by-convention key used to carry an ambiguous title-resolution's
# candidate list inside PendingClarification.arguments (itself a
# free-form dict, already JSON-serialized as-is by conversation_memory -
# see its _json_safe). Never a real tool argument: routes/agent.py and
# merge_ambiguous_task_reply strip it back out before it could ever reach
# tool_schemas.validate_tool_call/agent_service.execute_tool.
CLARIFICATION_CANDIDATES_KEY = "_clarification_candidates"

# Deterministic phrasing for tools with exactly one required argument.
_SINGLE_ARGUMENT_QUESTIONS: dict[str, str] = {
    "create_task": "What should the task title be?",
    "get_weather": "Which city would you like the weather for?",
    "mark_task_done": "Which task ID should I mark as done?",
    "delete_task": "Which task ID should I delete?",
}

_DUE_DATE_QUESTION = "What due date would you like? Please use the YYYY-MM-DD format."

# update_task is the only tool with two required arguments, so its
# question depends on which one(s) are missing. "due_date" here is never
# "due_date is missing/None" - it only ever appears when
# agent_decision.ToolDecision.needs_clarification_for flagged it (a
# reference to a due date that couldn't be confidently resolved to an
# explicit calendar date) - see missing_arguments below.
_UPDATE_TASK_QUESTIONS: dict[frozenset[str], str] = {
    frozenset({"task_id"}): "Which task ID would you like to update?",
    frozenset({"title"}): "What should the new title be?",
    frozenset({"task_id", "title"}): "Which task ID would you like to update, and what should the new title be?",
    frozenset({"due_date"}): _DUE_DATE_QUESTION,
    frozenset({"task_id", "due_date"}): f"Which task ID would you like to update? {_DUE_DATE_QUESTION}",
}


def missing_arguments(decision: ToolDecision) -> list[str]:
    """Return which required arguments for decision.selected_tool are missing.

    "Missing" means absent or explicitly None. Returns an empty list if
    no tool was selected at all (an unmatched message is never treated
    as a clarifiable tool call) or if the tool has no required arguments
    (e.g. list_tasks).

    update_task special case: "title" is only reported missing when
    NEITHER priority nor due_date was requested either - a priority-only
    or due-date-only update (no new title at all) is a valid request, as
    long as at least one of title/priority/due_date was actually touched.
    This reduces to exactly the old "title is always required" check for
    any message that never mentions priority/due_date (every existing
    test), so it changes nothing for those - see
    tool_schemas.REQUIRED_ARGUMENTS's own docstring note and
    tests/test_agent_decision.py.

    create_task/update_task also surface "due_date" as missing whenever
    decision.needs_clarification_for flags it - a due-date reference the
    rule-based provider detected but couldn't confidently resolve to an
    explicit calendar date (see agent_decision._extract_due_date). This
    is never derived from decision.arguments (there is no marker value
    living there) - see ToolDecision.needs_clarification_for's docstring.
    """
    if decision.selected_tool is None:
        return []

    required = tool_schemas.REQUIRED_ARGUMENTS.get(decision.selected_tool, {})
    missing = [name for name in required if decision.arguments.get(name) is None]

    if decision.selected_tool == "update_task" and "title" in missing:
        priority_given = decision.arguments.get("priority") is not None
        due_date_given = "due_date" in decision.arguments or "due_date" in decision.needs_clarification_for
        if priority_given or due_date_given:
            missing.remove("title")

    for field_name in decision.needs_clarification_for:
        if field_name not in missing:
            missing.append(field_name)

    return missing


def build_reason(selected_tool: str, missing: list[str]) -> str:
    """Deterministic, generic explanation of why clarification is needed."""
    return f"The {selected_tool} tool requires {', '.join(missing)}."


def build_clarification_question(selected_tool: str, missing: list[str]) -> str:
    """Deterministic, tool-specific question asking for the missing argument(s)."""
    if selected_tool == "update_task":
        return _UPDATE_TASK_QUESTIONS[frozenset(missing)]

    if selected_tool == "create_task" and "title" not in missing and "due_date" in missing:
        return _DUE_DATE_QUESTION

    return _SINGLE_ARGUMENT_QUESTIONS[selected_tool]


_CANCELLATION_PHRASES = {"cancel", "never mind", "stop"}


def is_cancellation(message: str) -> bool:
    """Whether a follow-up reply cancels the pending clarification."""
    normalized = message.strip().lower().strip(" .!?")
    return normalized in _CANCELLATION_PHRASES


# Tools whose execution is irreversible enough to require the user to
# explicitly say yes before it runs, instead of executing as soon as the
# decision is complete. Single source of truth is tool_schemas.DESTRUCTIVE_TOOLS
# - kept as a module-level alias here rather than a second literal set, so
# clarification.py and agent_planner.py can never silently drift apart on
# which tools count as destructive.
_DESTRUCTIVE_TOOLS = tool_schemas.DESTRUCTIVE_TOOLS


def requires_confirmation(selected_tool: str | None) -> bool:
    """Whether a complete decision for this tool must be confirmed before it runs."""
    return selected_tool in _DESTRUCTIVE_TOOLS


# Explicit, deterministic destructive delete/remove phrases (English and
# Romanian). Specific action phrases, not the bare word "delete" - this is
# what lets a task title like "delete old files" pass through untouched
# while "delete it"/"delete task"/etc. as their own clause/message do not.
# Single source of truth for two independent, deliberately conservative
# guards: agent_planner._has_destructive_intent (per-clause, blocks
# multi-step planning) and agent_decision's fallback-safety gate (whole
# message, blocks a silent fallback to rule_based). Both guards only ever
# block/ask-for-confirmation on a match - neither rewrites a tool, invents
# an argument, or executes anything directly; a false-positive match here
# just means an extra, safe refusal, never an unsafe action.
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


def contains_destructive_intent(text: str) -> bool:
    """Whether this text (a whole message, or a single clause of one)
    contains explicit destructive/delete intent phrasing.

    Case-insensitive, word-boundary aware (same approach as
    mentions_contextual_reference) - never a naive substring check, so a
    task title containing "delete" as part of a longer word can't
    falsely match.
    """
    lowered = text.lower()
    return any(re.search(r"\b" + re.escape(phrase) + r"\b", lowered) for phrase in _DESTRUCTIVE_INTENT_PHRASES)


# Deterministic, scope-limiting cues that a message is asking for more than
# one action, in English or Romanian. Single source of truth for two
# independent callers: agent_planner.should_attempt_planning (should this
# even be offered to the multi-step planner?) and agent_decision's
# fallback-safety gate (a multi-step-shaped message must not be silently
# handed to rule_based, which can only ever pick one tool).
_ENGLISH_CUES = ["then", "after that"]
_ROMANIAN_CUES = ["și apoi", "iar apoi", "apoi", "după aceea"]
MULTI_STEP_CUES = _ENGLISH_CUES + _ROMANIAN_CUES


def looks_multi_step(message: str) -> bool:
    """Whether the message contains an explicit cue that it's asking for
    more than one action, in English or Romanian.

    Case-insensitive and word-boundary aware, so this correctly handles
    Romanian diacritics (ș/ă) and never matches a cue as part of an
    unrelated word.
    """
    lowered = message.lower()
    return any(re.search(r"\b" + re.escape(cue) + r"\b", lowered) for cue in MULTI_STEP_CUES)


def build_confirmation_question(decision: ToolDecision, resolved_title: str | None = None) -> str:
    """Deterministic, tool-specific yes/no question for a destructive decision.

    resolved_title is only ever passed when task_id came from resolving a
    task_title reference (see app.services.task_resolution) - naming the
    resolved task makes it possible for the user to catch a wrong
    approximate match before confirming. Omitted (None) for an ordinary
    numeric task_id, which keeps the existing plain wording unchanged.
    """
    if decision.selected_tool == "delete_task":
        task_id = decision.arguments["task_id"]
        if resolved_title is not None:
            return f'Are you sure you want to delete task #{task_id} ("{resolved_title}")?'
        return f"Are you sure you want to delete task #{task_id}?"

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


def _merge_due_date_reply(merged: dict, message: str) -> bool:
    """Deterministically parse a follow-up reply to a due-date
    clarification - the reply is expected to just BE a date (e.g.
    "2026-08-20"), not a whole sentence. Sets merged["due_date"] and
    returns True only when the reply is a valid, explicit "YYYY-MM-DD"
    date (tool_schemas.is_valid_iso_date is the one shared definition of
    that - also used by tool_schemas.validate_tool_call and
    agent_decision._extract_due_date, so all three can never drift on
    what counts as a valid explicit date); otherwise `merged` is left
    untouched and this returns False, so the caller keeps waiting and
    re-asks - exactly like an unparseable numeric task-id reply already
    does today.
    """
    candidate = message.strip().strip(" .,!?")
    if tool_schemas.is_valid_iso_date(candidate):
        merged["due_date"] = candidate
        return True
    return False


def merge_reply(
    pending: PendingClarification, message: str
) -> tuple[dict[str, str | int | bool | None], tuple[str, ...]]:
    """Deterministically parse a follow-up reply and merge whatever it
    supplies into the pending decision's arguments. Never calls an LLM.

    Only fills in arguments that are still missing; anything the reply
    doesn't clearly answer is left as-is for another round.

    Returns (merged_arguments, needs_clarification_for). The second
    element carries "due_date" forward whenever it was pending and this
    reply still didn't resolve it to a valid explicit date - due_date's
    "missing-ness" (unlike task_id/title) can't be re-derived from
    `merged` alone (an unresolved due_date is simply absent from
    `merged`, indistinguishable from "never mentioned"), so the caller
    (routes/agent.py) must thread this into the reconstructed
    ToolDecision's own needs_clarification_for for missing_arguments to
    correctly re-ask instead of silently treating the request as
    complete with due_date silently dropped.
    """
    merged = dict(pending.arguments)
    missing = set(pending.missing)
    tool = pending.selected_tool
    needs_clarification: list[str] = []

    if tool in {"mark_task_done", "delete_task"} and "task_id" in missing:
        task_id = _extract_integer_from_reply(message)
        if task_id is not None:
            merged["task_id"] = task_id

    elif tool == "create_task" and "title" in missing:
        title = message.strip()
        if title:
            merged["title"] = title

    elif tool in {"create_task", "update_task"} and missing == {"due_date"}:
        if not _merge_due_date_reply(merged, message):
            needs_clarification.append("due_date")

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
        elif missing == {"task_id", "due_date"}:
            # Best-effort, single-field-per-reply: a reply that's a valid
            # date resolves due_date (task_id still pending next round);
            # otherwise try it as the task id (due_date still pending).
            if not _merge_due_date_reply(merged, message):
                needs_clarification.append("due_date")
                task_id = _extract_integer_from_reply(message)
                if task_id is not None:
                    merged["task_id"] = task_id

    return merged, tuple(needs_clarification)


# --- Ambiguous title-resolution clarification -------------------------------
#
# Separate from the missing-argument flow above: this handles the case
# where app.services.task_resolution matched more than one of the user's
# own tasks (or none at all) for a task_title reference, instead of a
# tool simply being missing a required argument outright.


def build_ambiguous_task_question(candidates: list[TaskCandidate]) -> str:
    """Deterministic question listing every candidate task, e.g.
    "I found 2 tasks matching that description: #3 'Client presentation',
    #7 'Client presentation slides'. Which one did you mean? Reply with
    the number or the exact title."
    """
    count = len(candidates)
    noun = "task" if count == 1 else "tasks"
    listed = ", ".join(f'#{c.task_id} "{c.title}"' for c in candidates)
    return (
        f"I found {count} {noun} matching that description: {listed}. "
        "Which one did you mean? Reply with the number (1, 2, ...) or the exact title."
    )


def build_not_found_task_question() -> str:
    """Deterministic, fixed question for when no task matched a title
    reference at all.
    """
    return "I couldn't find a task matching that description. Could you give me the task's exact title or its ID?"


def is_ambiguous_task_clarification(pending: PendingClarification) -> bool:
    """Whether a pending clarification is the ambiguous-title-match kind
    (carrying a candidate list) rather than an ordinary missing-argument
    clarification.
    """
    return CLARIFICATION_CANDIDATES_KEY in pending.arguments


def candidates_from_pending(pending: PendingClarification) -> list[TaskCandidate]:
    raw = pending.arguments.get(CLARIFICATION_CANDIDATES_KEY) or []
    return [TaskCandidate(task_id=item["task_id"], title=item["title"]) for item in raw]


def merge_ambiguous_task_reply(pending: PendingClarification, message: str) -> dict[str, str | int | bool | None]:
    """Deterministically resolve a follow-up reply to an ambiguous-title
    clarification against the candidate list stashed in
    pending.arguments[CLARIFICATION_CANDIDATES_KEY]. Never calls an LLM.

    Recognizes, in order: a 1-based list position ("2"), an exact
    (case-insensitive) title match, or a bare task id that matches one of
    the candidates. If none of those match, the candidate list is left
    untouched in the merged arguments (still carrying
    CLARIFICATION_CANDIDATES_KEY) - so the caller can tell resolution
    remains pending and must ask again, rather than mistaking this for a
    completed decision.
    """
    candidates = candidates_from_pending(pending)
    merged = dict(pending.arguments)
    stripped = message.strip()

    resolved_id: int | None = None

    if stripped.isdigit():
        position = int(stripped)
        if 1 <= position <= len(candidates):
            resolved_id = candidates[position - 1].task_id
        else:
            matching_id = [c.task_id for c in candidates if c.task_id == position]
            if matching_id:
                resolved_id = matching_id[0]
    else:
        normalized_reply = stripped.lower()
        matching_title = [c.task_id for c in candidates if c.title.strip().lower() == normalized_reply]
        if len(matching_title) == 1:
            resolved_id = matching_title[0]

    if resolved_id is not None:
        merged["task_id"] = resolved_id
        merged.pop(CLARIFICATION_CANDIDATES_KEY, None)
        merged.pop("task_title", None)

    return merged
