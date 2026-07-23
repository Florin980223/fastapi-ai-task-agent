"""Tests for POST /agent/execute, including the final_answer field.

Also covers end-to-end task resolution by title (see
app/services/task_resolution.py) - referring to a task by name instead of
its numeric id for mark_task_done/update_task/delete_task.
"""

import json


def _execute(client, message, conversation_id=None):
    """POST a message to /agent/execute and return the parsed JSON body."""
    body = {"message": message}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    response = client.post("/agent/execute", json=body)
    assert response.status_code == 200
    return response.json()


def test_create_task_returns_selected_tool_and_final_answer(client):
    data = _execute(client, "Add a task to buy milk")

    assert data["selected_tool"] == "create_task"
    assert data["result"]["title"] == "buy milk"
    assert data["final_answer"]


def test_list_tasks_returns_selected_tool_and_final_answer(client):
    _execute(client, "Add a task to buy milk")

    data = _execute(client, "Show me all tasks")

    assert data["selected_tool"] == "list_tasks"
    assert len(data["result"]) == 1
    assert data["final_answer"]


def test_mark_task_done_returns_correct_selected_tool(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    data = _execute(client, f"Mark task {task_id} as done")

    assert data["selected_tool"] == "mark_task_done"
    assert data["result"]["done"] is True
    assert data["final_answer"]


def test_update_task_returns_correct_selected_tool(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    data = _execute(client, f"Update task {task_id} to Buy oat milk")

    assert data["selected_tool"] == "update_task"
    assert data["result"]["title"] == "Buy oat milk"
    assert data["final_answer"]


def test_delete_todo_message_is_recognized_as_delete_task(client):
    # Regression test for the "todo" keyword bug: create_task's "todo"
    # keyword used to be checked before delete_task's, so "Delete todo
    # 3" was wrongly classified as create_task.
    _execute(client, "Add a task to buy milk")
    _execute(client, "Add a task to walk the dog")
    _execute(client, "Add a task to read a book")

    asked = _execute(client, "Delete todo 3")

    assert asked["selected_tool"] == "delete_task"
    assert asked["needs_confirmation"] is True
    assert asked["result"] is None

    data = _execute(client, "yes", conversation_id=asked["conversation_id"])

    assert data["selected_tool"] == "delete_task"
    assert data["needs_confirmation"] is False
    assert data["result"] == {"status": "deleted", "task_id": 3}
    assert data["final_answer"]


def test_deleting_missing_task_returns_friendly_final_answer(client):
    asked = _execute(client, "Delete task 999")

    assert asked["needs_confirmation"] is True

    data = _execute(client, "yes", conversation_id=asked["conversation_id"])

    assert data["selected_tool"] == "delete_task"
    assert data["needs_confirmation"] is False
    assert data["result"] == {"error": "Task 999 was not found."}
    assert data["final_answer"] == "Task 999 was not found."


def test_agent_created_task_is_owned_by_the_authenticated_user(client, other_user_headers):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    # Bob (a different authenticated user) cannot see or reach it.
    assert client.get(f"/tasks/{task_id}", headers=other_user_headers).status_code == 404
    bob_tasks = client.get("/tasks", headers=other_user_headers).json()
    assert bob_tasks == []

    # Alice can.
    assert client.get(f"/tasks/{task_id}").status_code == 200


def test_agent_list_tasks_only_returns_the_authenticated_users_tasks(client, other_user_headers):
    client.post("/tasks", json={"title": "Bob task"}, headers=other_user_headers)
    _execute(client, "Add a task to buy milk")

    data = _execute(client, "Show me all tasks")

    assert data["selected_tool"] == "list_tasks"
    assert [task["title"] for task in data["result"]] == ["buy milk"]


def test_get_weather_final_answer_uses_mocked_weather_service(client, monkeypatch):
    # Avoid calling the real Open-Meteo API: replace weather_service's
    # function with a fake that returns a fixed result.
    def fake_get_weather_for_city(city):
        return {
            "city": "London",
            "country": "United Kingdom",
            "latitude": 51.5,
            "longitude": -0.1,
            "current_temperature": 20.0,
            "wind_speed": 10.0,
            "weather_code": 1,
        }

    monkeypatch.setattr(
        "app.services.weather_service.get_weather_for_city",
        fake_get_weather_for_city,
    )

    data = _execute(client, "What is the weather in London?")

    assert data["selected_tool"] == "get_weather"
    assert data["result"]["city"] == "London"
    assert data["final_answer"] == "It's currently 20.0°C in London with wind speed 10.0 km/h."


# --- Task resolution by title (end-to-end) -----------------------------------


def test_mark_task_done_by_exact_title_executes_immediately(client):
    _execute(client, "Add a task to prepare client presentation")

    data = _execute(client, "Mark the prepare client presentation task as done")

    assert data["selected_tool"] == "mark_task_done"
    assert data["needs_clarification"] is False
    assert data["result"]["done"] is True
    assert data["result"]["title"] == "prepare client presentation"


def test_mark_task_done_by_partial_title_resolves_to_single_match(client):
    _execute(client, "Add a task to Prepare final portfolio")

    data = _execute(client, "Mark the portfolio task as done")

    assert data["selected_tool"] == "mark_task_done"
    assert data["needs_clarification"] is False
    assert data["result"]["done"] is True
    assert data["result"]["title"] == "Prepare final portfolio"
    assert "Prepare final portfolio" in data["final_answer"]


def test_mark_task_done_natural_phrasing_with_leading_the_and_trailing_task_resolves_by_title(client):
    # Regression test for a reported smoke-test failure: "Mark the X task as
    # done." (leading "the", trailing "task" before "as done") must resolve
    # and execute exactly like the "Mark X as done" phrasing above - not be
    # treated as unknown intent.
    _execute(client, "Add a task to Prepare portfolio presentation Omega")
    _execute(client, "Add a task to Prepare client presentation Alpha")

    data = _execute(client, "Mark the portfolio presentation Omega task as done.")

    assert data["selected_tool"] == "mark_task_done"
    assert data["needs_clarification"] is False
    assert "couldn't figure out" not in data["final_answer"]
    assert data["result"]["done"] is True
    assert data["result"]["title"] == "Prepare portfolio presentation Omega"

    tasks = client.get("/tasks").json()
    unrelated = next(task for task in tasks if task["title"] == "Prepare client presentation Alpha")
    assert unrelated["done"] is False


def test_mark_task_done_by_ambiguous_title_returns_clarification_with_candidates(client):
    first = _execute(client, "Add a task to Client presentation")
    second = _execute(client, "Add a task to Team presentation")

    data = _execute(client, "Mark the presentation task as done")

    assert data["selected_tool"] == "mark_task_done"
    assert data["result"] is None
    assert data["needs_clarification"] is True
    assert data["needs_confirmation"] is False
    options = data["clarification_options"]
    assert options is not None
    assert {o["task_id"] for o in options} == {first["result"]["id"], second["result"]["id"]}

    # Neither task was touched.
    tasks = client.get("/tasks").json()
    assert all(task["done"] is False for task in tasks)


def test_mark_task_done_by_title_with_no_match_returns_not_found_clarification(client):
    _execute(client, "Add a task to buy milk")

    data = _execute(client, "Mark the quarterly report task as done")

    assert data["needs_clarification"] is True
    assert data["clarification_options"] is None
    assert "couldn't find a task" in data["final_answer"]


def test_ambiguous_clarification_followed_by_numeric_reply_executes_correct_task(client):
    _execute(client, "Add a task to Client presentation")
    _execute(client, "Add a task to Team presentation")

    asked = _execute(client, "Mark the presentation task as done")
    assert asked["needs_clarification"] is True
    second_candidate_id = asked["clarification_options"][1]["task_id"]

    data = _execute(client, "2", conversation_id=asked["conversation_id"])

    assert data["needs_clarification"] is False
    assert data["selected_tool"] == "mark_task_done"
    assert data["result"]["id"] == second_candidate_id
    assert data["result"]["done"] is True


def test_delete_task_by_title_still_requires_confirmation_naming_resolved_task(client):
    _execute(client, "Add a task to Prepare final portfolio")

    asked = _execute(client, "Delete the portfolio task")

    assert asked["needs_confirmation"] is True
    assert asked["needs_clarification"] is False
    assert "Prepare final portfolio" in asked["confirmation_question"]

    data = _execute(client, "yes", conversation_id=asked["conversation_id"])

    assert data["selected_tool"] == "delete_task"
    assert data["needs_confirmation"] is False
    assert data["result"]["status"] == "deleted"
    assert client.get("/tasks").json() == []


def test_delete_task_by_ambiguous_title_never_reaches_confirmation_gate(client):
    _execute(client, "Add a task to Client presentation")
    _execute(client, "Add a task to Team presentation")

    data = _execute(client, "Delete the presentation task")

    assert data["needs_confirmation"] is False
    assert data["needs_clarification"] is True
    assert data["clarification_options"] is not None

    # Nothing was deleted.
    assert len(client.get("/tasks").json()) == 2


def test_update_task_by_title_resolves_and_applies_new_title(client):
    _execute(client, "Add a task to Prepare final portfolio")

    data = _execute(client, "Rename the portfolio task to Prepare summer portfolio")

    assert data["selected_tool"] == "update_task"
    assert data["needs_clarification"] is False
    assert data["result"]["title"] == "Prepare summer portfolio"


def test_digit_based_flows_are_unaffected_by_title_resolution(client, monkeypatch):
    from app.services import task_service

    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("list_tasks must not be called for a digit-based task reference")

    monkeypatch.setattr(task_service, "list_tasks", _fail_if_called)

    data = _execute(client, f"Mark task {task_id} as done")

    assert data["result"]["done"] is True


def test_contextual_reference_still_resolves_via_last_task_id_not_title_matching(client, monkeypatch):
    from app.services import task_service

    created = _execute(client, "Add a task to buy milk")

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("list_tasks must not be called when task_id was already resolved from context")

    monkeypatch.setattr(task_service, "list_tasks", _fail_if_called)

    data = _execute(client, "Mark it as done", conversation_id=created["conversation_id"])

    assert data["selected_tool"] == "mark_task_done"
    assert data["result"]["done"] is True

    # A stray task_title extracted from "Mark it as done" (before context
    # resolved task_id) must never leak into the persisted trace - only
    # the resolved task_id belongs there, exactly like a numeric message.
    run = client.get(f"/agent/runs/{data['run_id']}").json()
    assert json.loads(run["steps"][0]["arguments_json"]) == {"task_id": created["result"]["id"]}


def test_title_resolution_only_searches_the_callers_own_tasks_end_to_end(client, other_user_headers):
    client.post("/tasks", json={"title": "Client presentation"}, headers=other_user_headers)

    data = _execute(client, "Mark the client presentation task as done")

    assert data["needs_clarification"] is True
    assert "couldn't find a task" in data["final_answer"]
