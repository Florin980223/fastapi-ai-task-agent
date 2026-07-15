"""Tests for POST /agent/execute, including the final_answer field."""


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
