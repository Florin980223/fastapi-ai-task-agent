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

from app.services import agent_decision
from app.services.agent_decision import decide_tool


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


def test_no_matching_tool_returns_none_selected_tool_and_empty_arguments():
    decision = decide_tool("hello there")

    assert decision.selected_tool is None
    assert decision.arguments == {}
    assert decision.reason == "No matching tool was found for this message."


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
