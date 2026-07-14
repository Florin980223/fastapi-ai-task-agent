"""Unit tests for the rule-based decision provider (no HTTP layer).

These pin down the ToolDecision contract (selected_tool, arguments,
reason) that any future decision provider (e.g. an LLM-based one)
would need to match.
"""

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
