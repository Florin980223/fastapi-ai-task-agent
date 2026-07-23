"""Tests for app.services.task_resolution - deterministic, non-LLM
matching of a free-text task reference against a user's own tasks.

resolve_task_reference() is pure and is tested here with plain Task
objects (never persisted) - no database needed. resolve_task_title_argument()
is the database-aware wrapper and is tested against the real test database
via the new_new_db_session/test_user_id fixtures from tests/conftest.py.
"""

from app.db_models import Task
from app.services import task_resolution, task_service
from app.services.task_resolution import (
    CLEAR_WINNER_MARGIN,
    FUZZY_MATCH_THRESHOLD,
    MAX_CANDIDATES,
    ResolutionOutcome,
    resolve_task_reference,
    resolve_task_title_argument,
)
from app.services.tool_decision import ToolDecision


def _task(task_id: int, title: str) -> Task:
    return Task(id=task_id, user_id="alice", title=title, description=None, done=False)


# --- resolve_task_reference: exact match -------------------------------------


def test_exact_case_insensitive_match_resolves_silently():
    tasks = [_task(1, "Prepare final portfolio"), _task(2, "Client presentation")]
    result = resolve_task_reference("client presentation", tasks)
    assert result.status == "resolved"
    assert result.task_id == 2
    assert result.title == "Client presentation"


def test_exact_match_beats_fuzzy_candidate():
    # "client presentation" is an exact match for task 2, and would also
    # fuzzy-match task 3 to some degree - exact must win outright, tier 3
    # must never even be consulted.
    tasks = [_task(2, "Client presentation"), _task(3, "Client presentation slides")]
    result = resolve_task_reference("client presentation", tasks)
    assert result.status == "resolved"
    assert result.task_id == 2


def test_two_tasks_with_identical_titles_is_ambiguous_with_two_candidates():
    tasks = [_task(1, "Testing"), _task(2, "Testing")]
    result = resolve_task_reference("testing", tasks)
    assert result.status == "ambiguous"
    assert {c.task_id for c in result.candidates} == {1, 2}


# --- resolve_task_reference: containment (tier 2) ----------------------------


def test_substring_containment_match_resolves_when_unique():
    tasks = [_task(1, "Prepare final portfolio"), _task(2, "Buy groceries")]
    result = resolve_task_reference("the portfolio task", tasks)
    assert result.status == "resolved"
    assert result.task_id == 1


def test_multiple_containment_matches_without_clear_winner_is_ambiguous():
    # Both titles contain "presentation" and are similarly-sized relative
    # to the reference - neither should win by CLEAR_WINNER_MARGIN.
    tasks = [_task(1, "Client presentation"), _task(2, "Team presentation")]
    result = resolve_task_reference("presentation", tasks)
    assert result.status == "ambiguous"
    assert {c.task_id for c in result.candidates} == {1, 2}


def test_containment_clear_winner_resolves_immediately():
    tasks = [_task(1, "Presentation"), _task(2, "A very long task about presentation logistics and catering")]
    result = resolve_task_reference("presentation", tasks)
    assert result.status == "resolved"
    assert result.task_id == 1


# --- resolve_task_reference: fuzzy (tier 3) ----------------------------------


def test_fuzzy_match_above_threshold_resolves_when_clear_winner():
    tasks = [_task(1, "Client presentation"), _task(2, "Buy groceries")]
    # Misspelled reference, no exact/containment match, but clearly closer
    # to task 1 than task 2.
    result = resolve_task_reference("client presentaton", tasks)
    assert result.status == "resolved"
    assert result.task_id == 1


def test_fuzzy_matches_close_in_score_are_ambiguous():
    tasks = [_task(1, "Client presentaton"), _task(2, "Cilent presentation")]
    result = resolve_task_reference("client presentation", tasks)
    assert result.status == "ambiguous"
    assert {c.task_id for c in result.candidates} == {1, 2}


def test_fuzzy_match_below_threshold_is_not_found():
    tasks = [_task(1, "Buy groceries"), _task(2, "Walk the dog")]
    result = resolve_task_reference("client presentation", tasks)
    assert result.status == "not_found"
    assert result.candidates == ()


def test_fuzzy_threshold_constant_is_reasonable():
    # Pin the documented threshold/margin values so a future accidental
    # change is caught by this test rather than silently shipped.
    assert FUZZY_MATCH_THRESHOLD == 0.6
    assert CLEAR_WINNER_MARGIN == 0.15


# --- resolve_task_reference: edge cases --------------------------------------


def test_no_tasks_at_all_is_not_found():
    result = resolve_task_reference("anything", [])
    assert result.status == "not_found"


def test_empty_or_none_reference_is_not_found():
    tasks = [_task(1, "Some task")]
    assert resolve_task_reference(None, tasks).status == "not_found"
    assert resolve_task_reference("", tasks).status == "not_found"
    assert resolve_task_reference("   ", tasks).status == "not_found"


def test_candidate_list_is_sorted_and_capped_at_max_candidates():
    tasks = [_task(i, "Presentation") for i in range(1, MAX_CANDIDATES + 3)]
    result = resolve_task_reference("presentation", tasks)
    assert result.status == "ambiguous"
    assert len(result.candidates) == MAX_CANDIDATES
    ids = [c.task_id for c in result.candidates]
    assert ids == sorted(ids)


def test_resolution_is_deterministic_across_repeated_calls():
    tasks = [_task(1, "Client presentaton"), _task(2, "Cilent presentation")]
    first = resolve_task_reference("client presentation", tasks)
    second = resolve_task_reference("client presentation", tasks)
    assert first == second


# --- resolve_task_title_argument: no-op / backward-compat cases -------------


def test_resolve_task_title_argument_is_noop_when_task_id_already_present(new_db_session, test_user_id, monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("list_tasks must not be called when task_id is already present")

    monkeypatch.setattr(task_service, "list_tasks", _fail_if_called)

    decision = ToolDecision(selected_tool="mark_task_done", arguments={"task_id": 5, "task_title": "ignored"}, reason="")
    outcome = resolve_task_title_argument(decision, new_db_session, test_user_id)

    assert outcome.status == "not_applicable"
    # task_id wins outright; the now-moot task_title is dropped so it
    # never leaks into execute_tool's arguments or the persisted trace.
    assert decision.arguments == {"task_id": 5}


def test_resolve_task_title_argument_is_noop_for_tools_outside_scope(new_db_session, test_user_id, monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("list_tasks must not be called for a tool outside TASK_ID_OR_TITLE_TOOLS")

    monkeypatch.setattr(task_service, "list_tasks", _fail_if_called)

    for tool in ("create_task", "list_tasks", "get_weather"):
        decision = ToolDecision(selected_tool=tool, arguments={"task_title": "something"}, reason="")
        outcome = resolve_task_title_argument(decision, new_db_session, test_user_id)
        assert outcome.status == "not_applicable"


def test_resolve_task_title_argument_is_noop_when_no_title_present(new_db_session, test_user_id, monkeypatch):
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("list_tasks must not be called when there is no task_title to resolve")

    monkeypatch.setattr(task_service, "list_tasks", _fail_if_called)

    decision = ToolDecision(selected_tool="mark_task_done", arguments={"task_id": None}, reason="")
    outcome = resolve_task_title_argument(decision, new_db_session, test_user_id)
    assert outcome.status == "not_applicable"


# --- resolve_task_title_argument: real resolution + isolation ---------------


def test_resolve_task_title_argument_resolves_and_mutates_decision(new_db_session, test_user_id):
    task_service.create_task(new_db_session, test_user_id, title="Prepare final portfolio", description=None)

    decision = ToolDecision(selected_tool="mark_task_done", arguments={"task_id": None, "task_title": "portfolio"}, reason="")
    outcome = resolve_task_title_argument(decision, new_db_session, test_user_id)

    assert outcome.status == "resolved"
    assert isinstance(decision.arguments["task_id"], int)
    assert "task_title" not in decision.arguments


def test_resolve_task_title_argument_only_searches_callers_own_tasks(new_db_session, test_user_id, other_test_user_id):
    task_service.create_task(new_db_session, other_test_user_id, title="Client presentation", description=None)

    decision = ToolDecision(
        selected_tool="mark_task_done", arguments={"task_id": None, "task_title": "client presentation"}, reason=""
    )
    outcome = resolve_task_title_argument(decision, new_db_session, test_user_id)

    # alice has no tasks at all - bob's identically-named task must never
    # be visible to alice's resolution attempt.
    assert outcome.status == "not_found"


def test_resolve_task_title_argument_ambiguous_returns_candidates(new_db_session, test_user_id):
    task_service.create_task(new_db_session, test_user_id, title="Testing", description=None)
    task_service.create_task(new_db_session, test_user_id, title="Testing", description=None)

    decision = ToolDecision(selected_tool="delete_task", arguments={"task_id": None, "task_title": "Testing"}, reason="")
    outcome = resolve_task_title_argument(decision, new_db_session, test_user_id)

    assert outcome.status == "ambiguous"
    assert len(outcome.candidates) == 2
    assert decision.arguments.get("task_id") is None
