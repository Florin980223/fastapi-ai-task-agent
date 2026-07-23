"""Unit tests for the rule-based decision provider (no HTTP layer), plus
the fallback-safety gate (_safe_to_fall_back) that decides whether an
Ollama/Anthropic provider failure may fall back to rule_based at all.

These pin down the ToolDecision contract (selected_tool, arguments,
reason) that any future decision provider (e.g. an LLM-based one)
would need to match. Provider-failure-triggers-the-gate integration tests
(a real Ollama/Anthropic failure reaching the gate) live in
test_ollama_decision_provider.py / test_anthropic_decision_provider.py;
this file tests the gate function itself directly, and that rule_based is
never invoked when the gate blocks.
"""

import pytest

from app.services import agent_decision, clarification, tool_schemas
from app.services.agent_decision import decide_tool
from app.services.tool_decision import ToolDecision


def test_create_task_decision_extracts_title():
    decision = decide_tool("Add a task to buy milk")

    assert decision.selected_tool == "create_task"
    assert decision.arguments == {"title": "buy milk"}


def test_list_tasks_decision_extracts_done_filter():
    assert decide_tool("Show me all tasks").arguments == {"done": None}
    assert decide_tool("show me completed tasks").arguments == {"done": True}
    assert decide_tool("list unfinished tasks").arguments == {"done": False}


def test_get_weather_decision_extracts_city():
    decision = decide_tool("What is the weather in London?")

    assert decision.selected_tool == "get_weather"
    assert decision.arguments == {"city": "London"}


def test_get_weather_decision_with_no_city_has_none_argument():
    decision = decide_tool("weather")

    assert decision.selected_tool == "get_weather"
    assert decision.arguments == {"city": None}


def test_mark_task_done_decision_extracts_task_id():
    decision = decide_tool("Mark task 1 as done")

    assert decision.selected_tool == "mark_task_done"
    assert decision.arguments == {"task_id": 1}


def test_update_task_decision_extracts_id_and_title():
    decision = decide_tool("Update task 2 to Buy oat milk")

    assert decision.selected_tool == "update_task"
    assert decision.arguments == {"task_id": 2, "title": "Buy oat milk"}


def test_delete_todo_message_is_recognized_as_delete_task():
    # Regression test for the "todo" keyword bug: create_task's "todo"
    # keyword used to be checked before delete_task's.
    decision = decide_tool("Delete todo 3")

    assert decision.selected_tool == "delete_task"
    assert decision.arguments == {"task_id": 3}


# --- Explicit task id must occupy an id-shaped grammatical position -----
# (task/todo + digits + an expected boundary: "as", "to", punctuation, or
# end of message) - a number that's part of a title must never be mistaken
# for an id, even when it directly follows the word "task".


def test_mark_task_done_decision_extracts_task_id_with_hash():
    decision = decide_tool("Mark task #9 as done")

    assert decision.selected_tool == "mark_task_done"
    assert decision.arguments == {"task_id": 9}


def test_delete_task_decision_extracts_task_id_with_hash():
    decision = decide_tool("Delete task #12")

    assert decision.selected_tool == "delete_task"
    assert decision.arguments == {"task_id": 12}


def test_update_task_decision_extracts_id_and_title_with_hash_and_q3_title():
    decision = decide_tool("Rename task #4 to Prepare Q3 report")

    assert decision.selected_tool == "update_task"
    assert decision.arguments == {"task_id": 4, "title": "Prepare Q3 report"}


def test_update_task_decision_extracts_id_and_title_containing_q3():
    decision = decide_tool("Rename task 4 to Prepare Q3 report")

    assert decision.selected_tool == "update_task"
    assert decision.arguments == {"task_id": 4, "title": "Prepare Q3 report"}


def test_mark_task_done_title_containing_q3_extracts_no_task_id():
    decision = decide_tool("Mark Q3 report as done")

    assert decision.selected_tool == "mark_task_done"
    assert decision.arguments == {"task_id": None, "task_title": "Q3 report"}


def test_delete_task_title_containing_q2_extracts_no_task_id():
    decision = decide_tool("Delete the Q2 planning task")

    assert decision.selected_tool == "delete_task"
    assert decision.arguments == {"task_id": None, "task_title": "Q2 planning"}


def test_update_task_rename_by_title_to_new_title_containing_q3():
    decision = decide_tool("Rename the report task to Prepare Q3 report")

    assert decision.selected_tool == "update_task"
    assert decision.arguments == {"task_id": None, "task_title": "report", "title": "Prepare Q3 report"}


def test_year_immediately_after_task_is_not_mistaken_for_an_id():
    # Boundary case: "task" is immediately followed by digits, but those
    # digits are the start of a title ("2026 roadmap"), not an id - there's
    # no "as"/"to"/punctuation/end-of-message right after the number, so it
    # must not match as an id.
    decision = decide_tool("Mark task 2026 roadmap as done")

    assert decision.selected_tool == "mark_task_done"
    assert decision.arguments == {"task_id": None, "task_title": "2026 roadmap"}


def test_year_with_no_task_word_is_not_mistaken_for_an_id():
    decision = decide_tool("Mark Project 2026 roadmap as done")

    assert decision.selected_tool == "mark_task_done"
    assert decision.arguments == {"task_id": None, "task_title": "Project 2026 roadmap"}


def test_digits_glued_to_trailing_letters_are_not_mistaken_for_an_id():
    # "9abc" is not an id-shaped boundary (no "as"/"to"/punctuation/end
    # right after the digits) - task_id must stay None, and the digits
    # fall through to plain title text like any other title content.
    decision = decide_tool("Mark task 9abc as done")

    assert decision.selected_tool == "mark_task_done"
    assert decision.arguments == {"task_id": None, "task_title": "9abc"}


def test_mark_task_done_decision_extracts_task_title_when_no_digit():
    decision = decide_tool("Mark the client presentation task as done")

    assert decision.selected_tool == "mark_task_done"
    assert decision.arguments == {"task_id": None, "task_title": "client presentation"}


def test_mark_task_done_decision_handles_leading_the_and_trailing_task_phrasing():
    # Regression test for a reported smoke-test failure: a leading "the" and
    # a trailing "task" before "as done" must still resolve to the same
    # tool/title extraction as the simpler "Mark X as done" phrasing.
    decision = decide_tool("Mark the portfolio presentation Omega task as done.")

    assert decision.selected_tool == "mark_task_done"
    assert decision.arguments == {"task_id": None, "task_title": "portfolio presentation Omega"}


def test_mark_task_done_decision_extracts_full_title_without_leading_the():
    decision = decide_tool("Mark Prepare portfolio presentation Omega as done")

    assert decision.selected_tool == "mark_task_done"
    assert decision.arguments == {"task_id": None, "task_title": "Prepare portfolio presentation Omega"}


def test_mark_task_done_digit_based_behavior_is_unchanged_for_this_scenario():
    decision = decide_tool("Mark task 9 as done")

    assert decision.selected_tool == "mark_task_done"
    assert decision.arguments == {"task_id": 9}


def test_bare_task_title_with_no_verb_is_unknown_intent():
    # This is the actual message that produced "I couldn't figure out what
    # to do with that message" in the real agent_runs trace - a bare task
    # title with no actionable verb at all, not the "Mark the ... task as
    # done." sentence the ticket quoted. Confirms this is correct,
    # unchanged unknown-intent handling, not a bug.
    decision = decide_tool("Prepare portfolio presentation Omega")

    assert decision.selected_tool is None
    assert decision.arguments == {}


def test_delete_task_decision_extracts_task_title_when_no_digit():
    decision = decide_tool("Delete the old testing task")

    assert decision.selected_tool == "delete_task"
    assert decision.arguments == {"task_id": None, "task_title": "old testing"}


def test_update_task_decision_extracts_reference_and_new_title_when_no_digit():
    decision = decide_tool("Rename the portfolio task to Prepare final portfolio")

    assert decision.selected_tool == "update_task"
    assert decision.arguments == {"task_id": None, "task_title": "portfolio", "title": "Prepare final portfolio"}


def test_update_task_new_title_containing_to_is_captured_in_full_without_digit():
    decision = decide_tool("Update the drive task to Talk to Bob")

    assert decision.selected_tool == "update_task"
    assert decision.arguments == {"task_id": None, "task_title": "drive", "title": "Talk to Bob"}


def test_update_task_with_no_to_separator_and_no_digit_has_none_arguments():
    decision = decide_tool("Update the portfolio task")

    assert decision.selected_tool == "update_task"
    assert decision.arguments == {"task_id": None, "task_title": None, "title": None}


def test_generic_content_free_message_extracts_no_task_title():
    # Regression guard: a message with nothing but filler/articles must
    # collapse to task_title=None, never a stray leftover word like "a".
    assert decide_tool("Delete a task").arguments == {"task_id": None, "task_title": None}
    assert decide_tool("Mark a task as done").arguments == {"task_id": None, "task_title": None}


def test_digit_in_message_still_takes_priority_over_title_extraction():
    # Regression pin: a digit-containing message must produce exactly the
    # same arguments as before this feature existed - no task_title key
    # at all, since app.services.task_resolution never looks at it once
    # task_id is present.
    assert decide_tool("Mark task 1 as done").arguments == {"task_id": 1}
    assert decide_tool("Delete task 3").arguments == {"task_id": 3}
    assert decide_tool("Update task 2 to Buy oat milk").arguments == {"task_id": 2, "title": "Buy oat milk"}


def test_no_matching_tool_returns_none_selected_tool_and_empty_arguments():
    decision = decide_tool("hello there")

    assert decision.selected_tool is None
    assert decision.arguments == {}
    assert decision.reason == "No matching tool was found for this message."


# --- Priority (create_task/update_task) ----------------------------------


def test_create_task_recognizes_high_priority_hyphenated():
    decision = decide_tool("Create a high-priority task called Send client proposal due 2026-08-15")

    assert decision.selected_tool == "create_task"
    assert decision.arguments == {"title": "Send client proposal", "priority": "high", "due_date": "2026-08-15"}


def test_create_task_recognizes_low_priority_hyphenated():
    decision = decide_tool("Create a low-priority task called Review documentation")

    assert decision.selected_tool == "create_task"
    assert decision.arguments == {"title": "Review documentation", "priority": "low"}


def test_update_task_recognizes_trailing_high_priority_with_no_new_title():
    decision = decide_tool("Make the client contract task high priority")

    assert decision.selected_tool == "update_task"
    assert decision.arguments == {"task_id": None, "task_title": "client contract", "title": None, "priority": "high"}


def test_update_task_recognizes_trailing_medium_priority():
    decision = decide_tool("Change the Q4 report task to medium priority")

    assert decision.selected_tool == "update_task"
    assert decision.arguments == {"task_id": None, "task_title": "Q4 report", "title": None, "priority": "medium"}


@pytest.mark.parametrize(
    ("word", "canonical"),
    [("urgent", "high"), ("important", "high"), ("normal", "medium"), ("low", "low"), ("medium", "medium"), ("high", "high")],
)
def test_priority_synonyms_normalize_to_canonical_values(word, canonical):
    decision = decide_tool(f"Make the client contract task {word} priority")

    assert decision.arguments["priority"] == canonical


def test_priority_word_inside_a_genuine_title_is_never_extracted():
    decision = decide_tool("Create a task called High Priority Clients")

    assert decision.selected_tool == "create_task"
    assert decision.arguments == {"title": "High Priority Clients"}
    assert "priority" not in decision.arguments


def test_bare_synonym_word_with_no_priority_word_is_never_extracted():
    decision = decide_tool("Create a task called Urgent Customer Review")

    assert decision.selected_tool == "create_task"
    assert decision.arguments == {"title": "Urgent Customer Review"}
    assert "priority" not in decision.arguments


def test_priority_phrase_followed_by_more_title_words_is_never_extracted():
    decision = decide_tool("Rename a task to Normal Priority Process")

    assert decision.selected_tool == "update_task"
    assert decision.arguments == {"task_id": None, "task_title": None, "title": "Normal Priority Process"}
    assert "priority" not in decision.arguments


# --- Due date (create_task/update_task) ----------------------------------


def test_update_task_recognizes_deadline_to_explicit_date():
    decision = decide_tool("Set the Q4 report deadline to 2026-08-20")

    assert decision.selected_tool == "update_task"
    assert decision.arguments == {"task_id": None, "task_title": "Q4 report", "title": None, "due_date": "2026-08-20"}


def test_update_task_recognizes_changing_deadline_to_a_new_date():
    decision = decide_tool("Move the client contract deadline to 2026-09-01")

    assert decision.selected_tool == "update_task"
    assert decision.arguments == {
        "task_id": None,
        "task_title": "client contract",
        "title": None,
        "due_date": "2026-09-01",
    }


def test_update_task_recognizes_due_date_connector():
    decision = decide_tool("Update the client proposal task due date 2026-08-15")

    assert decision.selected_tool == "update_task"
    assert decision.arguments["due_date"] == "2026-08-15"


def test_update_task_recognizes_deadline_on_connector():
    decision = decide_tool("Set the client proposal task deadline on 2026-08-15")

    assert decision.selected_tool == "update_task"
    assert decision.arguments["due_date"] == "2026-08-15"


def test_clear_the_deadline_produces_explicit_null_due_date():
    decision = decide_tool("Clear the deadline for the client contract task")

    assert decision.selected_tool == "update_task"
    assert decision.arguments == {"task_id": None, "task_title": "client contract", "title": None, "due_date": None}
    assert decision.needs_clarification_for == ()


def test_remove_the_due_date_also_produces_explicit_null():
    decision = decide_tool("Remove the due date for the Q4 report task")

    assert decision.arguments["due_date"] is None


def test_combined_rename_and_priority_with_no_actual_new_title():
    decision = decide_tool("Rename the Q4 report task and make it high priority")

    assert decision.selected_tool == "update_task"
    assert decision.arguments == {"task_id": None, "task_title": "Q4 report", "title": None, "priority": "high"}


def test_combined_priority_and_due_date_update():
    decision = decide_tool("Update the client proposal task to high priority and due 2026-08-15")

    assert decision.selected_tool == "update_task"
    assert decision.arguments == {
        "task_id": None,
        "task_title": "client proposal",
        "title": None,
        "priority": "high",
        "due_date": "2026-08-15",
    }


def test_combined_priority_and_due_date_on_create():
    decision = decide_tool("Create a high-priority task called Prepare handoff due 2026-08-15")

    assert decision.selected_tool == "create_task"
    assert decision.arguments == {"title": "Prepare handoff", "priority": "high", "due_date": "2026-08-15"}


# --- Due-date title protection --------------------------------------------


def test_review_deadline_policy_is_a_plain_title_not_a_planning_command():
    decision = decide_tool("Create a task called Review deadline policy")

    assert decision.selected_tool == "create_task"
    assert decision.arguments == {"title": "Review deadline policy"}
    assert decision.needs_clarification_for == ()


def test_due_diligence_review_is_a_plain_title_not_a_planning_command():
    decision = decide_tool("Create a task called Due diligence review")

    assert decision.selected_tool == "create_task"
    assert decision.arguments == {"title": "Due diligence review"}
    assert decision.needs_clarification_for == ()


def test_rename_to_deadline_documentation_is_a_plain_title():
    decision = decide_tool("Rename the task to Deadline documentation")

    assert decision.selected_tool == "update_task"
    assert decision.arguments == {"task_id": None, "task_title": None, "title": "Deadline documentation"}
    assert decision.needs_clarification_for == ()


# --- Unclear/malformed due dates - deterministic clarification, never a --
# marker value reaching arguments (see tool_decision.ToolDecision) --------


def test_relative_date_triggers_due_date_clarification_not_silent_guess():
    decision = decide_tool("Set the client contract deadline to next Friday")

    assert decision.selected_tool == "update_task"
    assert decision.needs_clarification_for == ("due_date",)
    # Never guessed, never present as any value at all - absent entirely.
    assert "due_date" not in decision.arguments


def test_malformed_calendar_date_triggers_clarification():
    decision = decide_tool("Update the client proposal task due 2026-13-45")

    assert decision.selected_tool == "update_task"
    assert decision.needs_clarification_for == ("due_date",)
    assert "due_date" not in decision.arguments


def test_create_task_with_unclear_due_date_also_asks_for_clarification():
    decision = decide_tool("Create a task called Send proposal due date next Friday")

    assert decision.selected_tool == "create_task"
    assert decision.needs_clarification_for == ("due_date",)
    assert decision.arguments["title"] == "Send proposal"
    assert "due_date" not in decision.arguments


def test_the_internal_unclear_marker_can_never_reach_arguments_or_execution():
    """Structural proof for the adjustment-2 safety requirement: there is
    no marker *value* anywhere - unclear due-date is signaled exclusively
    via ToolDecision.needs_clarification_for, a field Anthropic/Ollama
    never populate (they construct ToolDecision only from the model's own
    JSON tool-call arguments - see anthropic_decision_provider.decide_tool/
    ollama_decision_provider.decide_tool). Confirms both that arguments
    never contains anything for the unresolved field, and that
    agent_service.execute_tool (which only ever reads decision.arguments)
    has nothing to accidentally act on.
    """
    decision = decide_tool("Set the client contract deadline to next Friday")

    assert "due_date" not in decision.arguments
    assert all(not isinstance(v, str) or "unclear" not in v.lower() for v in decision.arguments.values())


# --- update_task's "at least one mutation" requirement --------------------


def test_update_task_priority_only_change_does_not_ask_for_a_title():
    # Simulates the post-title-resolution state (task_id already filled
    # in from task_title by app.services.task_resolution, which always
    # runs before missing_arguments - see routes/agent.py) - this is the
    # layer requirement 1's "at least one mutation" rule actually lives.
    decision = ToolDecision(
        selected_tool="update_task", arguments={"task_id": 5, "title": None, "priority": "high"}, reason="r"
    )
    assert clarification.missing_arguments(decision) == []


def test_update_task_due_date_only_change_does_not_ask_for_a_title():
    decision = ToolDecision(
        selected_tool="update_task", arguments={"task_id": 5, "title": None, "due_date": "2026-08-20"}, reason="r"
    )
    assert clarification.missing_arguments(decision) == []


def test_update_task_due_date_clear_also_does_not_ask_for_a_title():
    decision = ToolDecision(
        selected_tool="update_task", arguments={"task_id": 5, "title": None, "due_date": None}, reason="r"
    )
    assert clarification.missing_arguments(decision) == []


def test_update_task_selector_with_no_mutation_fails_safely():
    # requirement 1: "selector with no mutation" - task_id present, but
    # none of title/priority/due_date given at all - must still ask,
    # never silently execute a no-op update.
    decision = ToolDecision(selected_tool="update_task", arguments={"task_id": 5, "title": None}, reason="r")
    assert clarification.missing_arguments(decision) == ["title"]


def test_update_task_mutation_with_no_selector_fails_safely():
    # requirement 1: "mutation with no selector" - neither task_id nor
    # task_title given at all - must ask for the task, never guess.
    decision = ToolDecision(selected_tool="update_task", arguments={"task_id": None, "priority": "high"}, reason="r")
    assert clarification.missing_arguments(decision) == ["task_id"]


def test_update_task_invalid_priority_fails_safely():
    # requirement 1: "invalid priority" - never silently coerced.
    with pytest.raises(tool_schemas.ToolCallValidationError, match="wrong type"):
        tool_schemas.validate_tool_call("update_task", {"task_id": 5, "priority": "urgent-ish"})


def test_update_task_invalid_calendar_date_fails_safely():
    # requirement 1: "invalid calendar date" - never silently coerced.
    with pytest.raises(tool_schemas.ToolCallValidationError, match="wrong type"):
        tool_schemas.validate_tool_call("update_task", {"task_id": 5, "due_date": "2026-13-45"})


def test_update_task_with_neither_title_nor_priority_nor_due_date_still_asks_for_title():
    # Regression pin: an ordinary update message that never mentions
    # priority/due_date keeps its exact old behavior, all the way from
    # decide_tool (not just missing_arguments in isolation).
    decision = decide_tool("Update the portfolio task")

    assert clarification.missing_arguments(decision) == ["task_id", "title"]


# --- _safe_to_fall_back: the fallback-safety gate -----------------------


def test_multi_step_shaped_message_is_not_safe_to_fall_back():
    assert agent_decision._safe_to_fall_back("Create a task to buy milk and then show me all tasks", None) is False


def test_destructive_message_is_not_safe_to_fall_back():
    assert agent_decision._safe_to_fall_back("Delete task 1", None) is False


def test_contextual_reference_is_not_safe_to_fall_back():
    assert agent_decision._safe_to_fall_back("Mark it as done", None) is False


def test_argument_failure_categories_are_not_safe_to_fall_back():
    assert agent_decision._safe_to_fall_back("Update task 1 to New title", "wrong_type") is False
    assert agent_decision._safe_to_fall_back("Update task 1 to New title", "unknown_argument") is False


def test_ambiguous_message_matching_multiple_rules_is_not_safe_to_fall_back():
    # "update" (update_task) and "done" (mark_task_done) both match -
    # rule_based can't cleanly agree with itself on which tool applies.
    message = "update and mark this done"
    assert agent_decision._count_matching_rules(message) > 1
    assert agent_decision._safe_to_fall_back(message, None) is False


def test_plain_single_step_message_is_safe_to_fall_back():
    assert agent_decision._safe_to_fall_back("Add a task to buy milk", None) is True
    assert agent_decision._safe_to_fall_back("Add a task to buy milk", "unknown_tool") is True
    assert agent_decision._safe_to_fall_back("Add a task to buy milk", "malformed_json") is True


def test_false_positive_destructive_phrasing_does_not_crash_and_stays_blocked(monkeypatch):
    """"Do not delete the task", "explain how delete works", and a quoted
    hypothetical are all edge cases the phrase list might or might not
    literally match - what matters is the outcome always stays safe
    (blocked), never something in the "must never" list (never falls
    through to rule_based re-deriving a destructive action from raw text).
    """

    def fail_if_called(message):
        raise AssertionError("rule_based must never be consulted for these edge-case messages")

    monkeypatch.setattr(agent_decision, "DECISION_PROVIDER", "ollama")
    monkeypatch.setattr(agent_decision, "_decide_tool_rule_based", fail_if_called)

    from app.services import ollama_decision_provider

    def raise_network_error(payload):
        raise ConnectionError("simulated provider outage")

    monkeypatch.setattr(ollama_decision_provider, "_call_ollama", raise_network_error)

    for message in [
        "Do not delete the task and then show me all tasks",
        "Explain how delete works and then show me all tasks",
        'If I said "delete task 3" and then showed my tasks, what would happen?',
    ]:
        with pytest.raises(agent_decision.UnsafeFallbackError):
            agent_decision.decide_tool(message)


def test_rule_based_is_never_called_when_the_gate_blocks(monkeypatch):
    def fail_if_called(message):
        raise AssertionError("rule_based must never be consulted when the fallback-safety gate blocks")

    from app.services import ollama_decision_provider

    monkeypatch.setattr(agent_decision, "DECISION_PROVIDER", "ollama")
    monkeypatch.setattr(agent_decision, "_decide_tool_rule_based", fail_if_called)
    monkeypatch.setattr(ollama_decision_provider, "_call_ollama", lambda payload: (_ for _ in ()).throw(ConnectionError("down")))

    with pytest.raises(agent_decision.UnsafeFallbackError):
        agent_decision.decide_tool("Delete task 1")
