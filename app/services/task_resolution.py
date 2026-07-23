"""Deterministic, non-LLM resolution of a free-text task reference (e.g.
"the portfolio task") to one of the caller's own tasks.

resolve_task_reference() is pure - it never touches the database. Callers
must fetch the candidate task list themselves via task_service.list_tasks
(db, user_id), which is what makes cross-user isolation structurally
impossible to get wrong here: this module never sees another user's tasks,
because it never has a user_id or a database session to go looking with.

Matching runs in three strict, ordered tiers - a lower tier is never
consulted once a higher tier produces any match at all:

1. Exact match (case-insensitive, stripped). More than one exact match
   (duplicate titles) is ambiguous - never silently broken by falling
   through to fuzzy scoring to "settle" an exact tie.
2. Substring/token-containment match.
3. Fuzzy match (difflib.SequenceMatcher ratio) above FUZZY_MATCH_THRESHOLD.

An approximate (tier 2/3) match is only ever resolved immediately - never
just "the best guess" - when it is simultaneously: above its tier's
minimum qualifying threshold, the single best-scoring candidate, AND
separated from the runner-up by at least CLEAR_WINNER_MARGIN. Any one of
those failing returns "ambiguous" with the qualifying candidates, never a
silent pick.

resolve_task_title_argument() is the thin, database-aware wrapper that
routes/agent.py actually calls: it no-ops (status="not_applicable") for
any tool outside TASK_ID_OR_TITLE_TOOLS, and - critically for backward
compatibility and avoiding a wasted query - no-ops without touching the
database at all whenever decision.arguments already has a task_id, or has
no task_title to resolve.
"""

import difflib
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.db_models import Task
from app.services import task_service, tool_schemas
from app.services.tool_decision import ToolDecision

# Minimum difflib.SequenceMatcher.ratio() for a fuzzy (tier 3) candidate to
# qualify at all. Below this, the candidate is discarded outright - never
# offered as a clarification option, since it's not a plausible match.
FUZZY_MATCH_THRESHOLD = 0.6

# Minimum score separation between the best and second-best qualifying
# candidate (within the same tier) required to resolve immediately instead
# of asking for clarification. Applies to both tier 2 (containment) and
# tier 3 (fuzzy) scoring.
CLEAR_WINNER_MARGIN = 0.15

# Maximum number of candidates surfaced in an ambiguous-match clarification,
# to keep the question readable. A UX cap only - never a safety boundary.
MAX_CANDIDATES = 5


@dataclass(frozen=True)
class TaskCandidate:
    """One task offered as a possible match, safe to show to the user."""

    task_id: int
    title: str


@dataclass(frozen=True)
class ResolutionResult:
    """Result of matching a free-text reference against a candidate list.

    status is one of "resolved", "ambiguous", "not_found". task_id/title
    are only set on "resolved"; candidates are only non-empty on
    "ambiguous".
    """

    status: str
    task_id: int | None = None
    title: str | None = None
    candidates: tuple[TaskCandidate, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ResolutionOutcome:
    """Result of attempting to resolve decision.arguments["task_title"]
    for a specific tool decision. status is one of "not_applicable"
    (nothing to resolve, decision.arguments is untouched), "resolved"
    (decision.arguments["task_id"] has been filled in), "ambiguous", or
    "not_found".
    """

    status: str
    task_id: int | None = None
    title: str | None = None
    candidates: tuple[TaskCandidate, ...] = field(default_factory=tuple)


def _normalize(text: str) -> str:
    return text.strip().lower()


# Common filler words stripped only for token-containment purposes (tier 2)
# - never applied to the exact-match tier, and never used to rewrite what's
# shown back to the user. Lets a reference like "the portfolio task" match
# a title like "Prepare final portfolio" via its one substantive token
# ("portfolio") without requiring the caller to have already stripped
# these words itself.
_CONTAINMENT_STOPWORDS = {"the", "a", "an", "to", "task", "this", "that"}


def _significant_tokens(normalized_reference: str) -> list[str]:
    tokens = [token for token in normalized_reference.split() if token not in _CONTAINMENT_STOPWORDS]
    return tokens or normalized_reference.split()


def _exact_matches(reference: str, tasks: list[Task]) -> list[Task]:
    normalized_reference = _normalize(reference)
    return [task for task in tasks if _normalize(task.title) == normalized_reference]


def _containment_score(reference: str, title: str) -> float:
    """How much of the (normalized) title the reference "covers" - closer
    to 1 means the reference is nearly as long as the whole title, i.e. a
    tighter, more specific reference to that particular task. Only ever
    called on a pair that has already passed the binary containment test
    below - this is purely for ranking multiple qualifying candidates.
    """
    if not title:
        return 0.0
    return min(len(reference), len(title)) / len(title)


def _containment_matches(reference: str, tasks: list[Task]) -> list[tuple[Task, float]]:
    normalized_reference = _normalize(reference)
    significant_tokens = _significant_tokens(normalized_reference)

    matches: list[tuple[Task, float]] = []
    for task in tasks:
        normalized_title = _normalize(task.title)
        contains = (
            normalized_reference in normalized_title
            or normalized_title in normalized_reference
            or all(token in normalized_title for token in significant_tokens)
        )
        if contains:
            matches.append((task, _containment_score(normalized_reference, normalized_title)))
    return matches


def _fuzzy_matches(reference: str, tasks: list[Task]) -> list[tuple[Task, float]]:
    normalized_reference = _normalize(reference)
    matches: list[tuple[Task, float]] = []
    for task in tasks:
        ratio = difflib.SequenceMatcher(None, normalized_reference, _normalize(task.title)).ratio()
        if ratio >= FUZZY_MATCH_THRESHOLD:
            matches.append((task, ratio))
    return matches


def _to_candidates(tasks: list[Task]) -> tuple[TaskCandidate, ...]:
    ordered = sorted(tasks, key=lambda task: task.id)
    return tuple(TaskCandidate(task_id=task.id, title=task.title) for task in ordered[:MAX_CANDIDATES])


def _resolve_scored_tier(scored: list[tuple[Task, float]]) -> ResolutionResult:
    """Shared decision logic for tiers 2 and 3: exactly one qualifying
    candidate resolves outright; multiple candidates resolve only if the
    top score clears CLEAR_WINNER_MARGIN over the runner-up - otherwise
    ambiguous. Sorted by (-score, task_id) first for fully deterministic
    tie-breaking.
    """
    if not scored:
        return ResolutionResult(status="not_found")

    if len(scored) == 1:
        task, _ = scored[0]
        return ResolutionResult(status="resolved", task_id=task.id, title=task.title)

    ranked = sorted(scored, key=lambda pair: (-pair[1], pair[0].id))
    top_task, top_score = ranked[0]
    _, second_score = ranked[1]

    if top_score - second_score >= CLEAR_WINNER_MARGIN:
        return ResolutionResult(status="resolved", task_id=top_task.id, title=top_task.title)

    candidates = _to_candidates([task for task, _ in ranked])
    return ResolutionResult(status="ambiguous", candidates=candidates)


def resolve_task_reference(reference: str | None, tasks: list[Task]) -> ResolutionResult:
    """Match a free-text task reference against the caller's own tasks.

    Pure function - no database access, no randomness. Same inputs always
    produce the same output.
    """
    if not reference or not reference.strip() or not tasks:
        return ResolutionResult(status="not_found")

    exact = _exact_matches(reference, tasks)
    if len(exact) == 1:
        task = exact[0]
        return ResolutionResult(status="resolved", task_id=task.id, title=task.title)
    if len(exact) > 1:
        return ResolutionResult(status="ambiguous", candidates=_to_candidates(exact))

    containment = _containment_matches(reference, tasks)
    if containment:
        return _resolve_scored_tier(containment)

    fuzzy = _fuzzy_matches(reference, tasks)
    return _resolve_scored_tier(fuzzy)


def resolve_task_title_argument(decision: ToolDecision, db: Session, user_id: str) -> ResolutionOutcome:
    """Resolve decision.arguments["task_title"] into a task_id, if needed.

    No-ops (status="not_applicable", decision untouched, no database
    query at all) when: the tool isn't one of TASK_ID_OR_TITLE_TOOLS, a
    task_id is already present (this is what guarantees zero added query
    cost and byte-identical behavior for every existing numeric-id
    message), or there's no task_title to resolve either - in that last
    case the caller falls through to the ordinary missing-argument
    clarification, unchanged.

    On a successful resolution, mutates decision.arguments in place -
    sets task_id, removes task_title - the same in-place-mutation idiom
    clarification.resolve_remembered_task_id already uses, so every
    downstream consumer (missing_arguments, validate_tool_call, the
    confirmation gate, execute_tool) sees a plain, familiar task_id
    argument exactly as if the user had typed the numeric id.
    """
    if decision.selected_tool not in tool_schemas.TASK_ID_OR_TITLE_TOOLS:
        return ResolutionOutcome(status="not_applicable")

    if decision.arguments.get("task_id") is not None:
        # task_id is already known - a digit in the message, or filled in
        # from conversation context (clarification.resolve_remembered_task_id,
        # which runs before this). Never touch the database in this case
        # (the whole point of this check), but do drop a stray task_title
        # if one is also present: a no-digit message like "mark it done"
        # still has agent_decision extract a (now-moot) task_title, and
        # once "it" resolves task_id from context that leftover key must
        # not linger into execute_tool's arguments or the persisted trace.
        decision.arguments.pop("task_title", None)
        return ResolutionOutcome(status="not_applicable")

    reference = decision.arguments.get("task_title")
    if not reference:
        return ResolutionOutcome(status="not_applicable")

    tasks = task_service.list_tasks(db, user_id)
    result = resolve_task_reference(reference, tasks)

    if result.status == "resolved":
        decision.arguments["task_id"] = result.task_id
        decision.arguments.pop("task_title", None)
        return ResolutionOutcome(status="resolved", task_id=result.task_id, title=result.title)

    return ResolutionOutcome(status=result.status, candidates=result.candidates)
