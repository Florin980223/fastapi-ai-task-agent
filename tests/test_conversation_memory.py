"""Tests for multi-turn clarification memory on POST /agent/execute.

A short follow-up reply like "3" must be merged into the pending
decision deterministically, without ever going through a decision
provider (no Anthropic/Ollama/rule-based call for a bare reply).
"""

import uuid

from app.services import weather_service


def _execute(client, message, conversation_id=None):
    body = {"message": message}
    if conversation_id is not None:
        body["conversation_id"] = str(conversation_id)
    response = client.post("/agent/execute", json=body)
    assert response.status_code == 200
    return response.json()


def test_delete_task_clarification_followed_by_id(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    asked = _execute(client, "Delete a task")
    assert asked["needs_clarification"] is True
    conversation_id = asked["conversation_id"]

    answered = _execute(client, str(task_id), conversation_id=conversation_id)

    assert answered["needs_clarification"] is False
    assert answered["selected_tool"] == "delete_task"
    assert answered["result"] == {"status": "deleted", "task_id": task_id}
    assert client.get("/tasks").json() == []


def test_mark_task_done_clarification_followed_by_id(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    asked = _execute(client, "Mark a task as done")
    conversation_id = asked["conversation_id"]

    answered = _execute(client, f"task {task_id}", conversation_id=conversation_id)

    assert answered["needs_clarification"] is False
    assert answered["selected_tool"] == "mark_task_done"
    assert answered["result"]["done"] is True


def test_create_task_clarification_followed_by_title(client):
    asked = _execute(client, "Create a task")
    conversation_id = asked["conversation_id"]

    answered = _execute(client, "Buy milk", conversation_id=conversation_id)

    assert answered["needs_clarification"] is False
    assert answered["selected_tool"] == "create_task"
    assert answered["result"]["title"] == "Buy milk"


def test_get_weather_clarification_followed_by_city(client, monkeypatch):
    def fake_get_weather_for_city(city):
        return {
            "city": city,
            "country": "United Kingdom",
            "latitude": 51.5,
            "longitude": -0.1,
            "current_temperature": 20.0,
            "wind_speed": 10.0,
            "weather_code": 1,
        }

    monkeypatch.setattr(weather_service, "get_weather_for_city", fake_get_weather_for_city)

    asked = _execute(client, "What is the weather?")
    conversation_id = asked["conversation_id"]

    answered = _execute(client, "London", conversation_id=conversation_id)

    assert answered["needs_clarification"] is False
    assert answered["selected_tool"] == "get_weather"
    assert answered["result"]["city"] == "London"


def test_update_task_requires_multiple_follow_up_values(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    asked = _execute(client, "Update a task")
    assert asked["clarification_question"] == (
        "Which task ID would you like to update, and what should the new title be?"
    )
    conversation_id = asked["conversation_id"]

    after_id = _execute(client, str(task_id), conversation_id=conversation_id)
    assert after_id["needs_clarification"] is True
    assert after_id["clarification_question"] == "What should the new title be?"
    # Not executed yet, so the title must still be unchanged.
    assert client.get("/tasks").json()[0]["title"] == "buy milk"

    completed = _execute(client, "Buy oat milk", conversation_id=conversation_id)
    assert completed["needs_clarification"] is False
    assert completed["selected_tool"] == "update_task"
    assert completed["result"]["title"] == "Buy oat milk"


def test_incomplete_follow_up_does_not_mutate_state(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    asked = _execute(client, "Delete a task")
    conversation_id = asked["conversation_id"]

    reply = _execute(client, "banana", conversation_id=conversation_id)

    assert reply["needs_clarification"] is True
    assert reply["clarification_question"] == "Which task ID should I delete?"
    tasks = client.get("/tasks").json()
    assert any(task["id"] == task_id for task in tasks)


def test_pending_state_clears_after_successful_execution(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    asked = _execute(client, "Delete a task")
    conversation_id = asked["conversation_id"]
    _execute(client, str(task_id), conversation_id=conversation_id)

    # A later, unrelated message on the same conversation_id must go
    # through the normal provider flow, not be treated as another
    # delete_task follow-up.
    followup = _execute(client, "Show me all tasks", conversation_id=conversation_id)

    assert followup["selected_tool"] == "list_tasks"
    assert followup["needs_clarification"] is False


def test_two_conversation_ids_stay_isolated(client):
    asked_a = _execute(client, "Delete a task")
    conversation_a = asked_a["conversation_id"]

    asked_b = _execute(client, "Create a task")
    conversation_b = asked_b["conversation_id"]

    # Answer B first, then A, to prove they don't interfere.
    answered_b = _execute(client, "Buy milk", conversation_id=conversation_b)
    assert answered_b["selected_tool"] == "create_task"
    assert answered_b["needs_clarification"] is False

    answered_a = _execute(client, "5", conversation_id=conversation_a)
    assert answered_a["selected_tool"] == "delete_task"
    assert answered_a["needs_clarification"] is False
    assert answered_a["result"] == {"error": "Task 5 was not found."}


def test_generated_conversation_id_is_returned(client):
    data = _execute(client, "Add a task to buy milk")

    conversation_id = data["conversation_id"]
    assert conversation_id
    # Must be a valid UUID string.
    uuid.UUID(conversation_id)

    other = _execute(client, "Add a task to walk the dog")
    assert other["conversation_id"] != conversation_id


def test_cancellation_clears_pending_state_and_executes_nothing(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    asked = _execute(client, "Delete a task")
    conversation_id = asked["conversation_id"]

    cancelled = _execute(client, "cancel", conversation_id=conversation_id)

    assert cancelled["selected_tool"] is None
    assert cancelled["result"] is None
    assert cancelled["needs_clarification"] is False
    assert cancelled["clarification_question"] is None
    assert cancelled["reason"] == "The pending action was cancelled."
    assert cancelled["final_answer"] == "Okay, I cancelled the pending action."

    # The task was not deleted.
    assert any(task["id"] == task_id for task in client.get("/tasks").json())

    # A follow-up afterward is a fresh message, not a resumed clarification.
    followup = _execute(client, "3", conversation_id=conversation_id)
    assert followup["selected_tool"] is None
    assert followup["needs_clarification"] is False


def test_normal_request_without_pending_state_still_works(client):
    data = _execute(client, "Add a task to buy milk")

    assert data["needs_clarification"] is False
    assert data["selected_tool"] == "create_task"
    assert data["result"]["title"] == "buy milk"
    assert data["final_answer"] == 'Created task #1: "buy milk".'
