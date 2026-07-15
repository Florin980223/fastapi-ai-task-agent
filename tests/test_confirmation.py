"""Tests for the destructive-action confirmation gate on POST /agent/execute.

delete_task is the only destructive tool today: once its decision is
complete, it must not execute until the user explicitly confirms with a
deterministic reply ("yes"/"confirm"/"proceed"/"da"). A deterministic
cancellation reply ("no"/"cancel"/"never mind"/"stop"/"nu") clears the
pending confirmation without deleting anything. Confirmation replies are
parsed with plain string matching - no LLM provider is ever consulted for
them.
"""


def _execute(client, message, conversation_id=None):
    body = {"message": message}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    response = client.post("/agent/execute", json=body)
    assert response.status_code == 200
    return response.json()


def test_delete_task_does_not_execute_before_confirmation(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    asked = _execute(client, f"Delete task {task_id}")

    assert asked["selected_tool"] == "delete_task"
    assert asked["result"] is None
    assert asked["needs_clarification"] is False
    assert asked["needs_confirmation"] is True
    assert asked["confirmation_question"] == f"Are you sure you want to delete task #{task_id}?"
    assert any(task["id"] == task_id for task in client.get("/tasks").json())


def test_yes_executes_the_stored_delete(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    asked = _execute(client, f"Delete task {task_id}")
    conversation_id = asked["conversation_id"]

    confirmed = _execute(client, "yes", conversation_id=conversation_id)

    assert confirmed["needs_confirmation"] is False
    assert confirmed["confirmation_question"] is None
    assert confirmed["selected_tool"] == "delete_task"
    assert confirmed["result"] == {"status": "deleted", "task_id": task_id}
    assert client.get("/tasks").json() == []


def test_da_executes_the_stored_delete(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    asked = _execute(client, f"Delete task {task_id}")
    conversation_id = asked["conversation_id"]

    confirmed = _execute(client, "da", conversation_id=conversation_id)

    assert confirmed["needs_confirmation"] is False
    assert confirmed["result"] == {"status": "deleted", "task_id": task_id}
    assert client.get("/tasks").json() == []


def test_no_cancels_without_deletion(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    asked = _execute(client, f"Delete task {task_id}")
    conversation_id = asked["conversation_id"]

    cancelled = _execute(client, "no", conversation_id=conversation_id)

    assert cancelled["needs_confirmation"] is False
    assert cancelled["confirmation_question"] is None
    assert cancelled["selected_tool"] is None
    assert cancelled["result"] is None
    assert any(task["id"] == task_id for task in client.get("/tasks").json())

    # A follow-up afterward is a fresh message, not a resumed confirmation.
    followup = _execute(client, "3", conversation_id=conversation_id)
    assert followup["needs_confirmation"] is False


def test_ambiguous_reply_keeps_confirmation_pending(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    asked = _execute(client, f"Delete task {task_id}")
    conversation_id = asked["conversation_id"]

    reply = _execute(client, "banana", conversation_id=conversation_id)

    assert reply["needs_confirmation"] is True
    assert reply["confirmation_question"] == asked["confirmation_question"]
    assert any(task["id"] == task_id for task in client.get("/tasks").json())

    # Still waiting - a later "yes" on the same conversation completes it.
    confirmed = _execute(client, "yes", conversation_id=conversation_id)
    assert confirmed["result"] == {"status": "deleted", "task_id": task_id}


def test_incomplete_delete_asks_for_clarification_before_confirmation(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    asked = _execute(client, "Delete a task")

    assert asked["needs_clarification"] is True
    assert asked["needs_confirmation"] is False
    conversation_id = asked["conversation_id"]

    answered = _execute(client, str(task_id), conversation_id=conversation_id)

    assert answered["needs_clarification"] is False
    assert answered["needs_confirmation"] is True
    assert answered["result"] is None

    confirmed = _execute(client, "yes", conversation_id=conversation_id)
    assert confirmed["result"] == {"status": "deleted", "task_id": task_id}


def test_reference_based_delete_it_still_requires_confirmation(client):
    created = _execute(client, "Add a task to buy milk")
    conversation_id = created["conversation_id"]
    task_id = created["result"]["id"]

    asked = _execute(client, "Delete it", conversation_id=conversation_id)

    assert asked["needs_confirmation"] is True
    assert asked["result"] is None
    assert any(task["id"] == task_id for task in client.get("/tasks").json())

    confirmed = _execute(client, "yes", conversation_id=conversation_id)
    assert confirmed["result"] == {"status": "deleted", "task_id": task_id}
    assert client.get("/tasks").json() == []


def test_explicit_task_id_is_preserved_through_confirmation(client):
    _execute(client, "Add a task to buy milk")
    other = _execute(client, "Add a task to walk the dog")
    other_id = other["result"]["id"]

    asked = _execute(client, f"Delete task {other_id}")

    assert asked["confirmation_question"] == f"Are you sure you want to delete task #{other_id}?"

    confirmed = _execute(client, "yes", conversation_id=asked["conversation_id"])

    assert confirmed["result"] == {"status": "deleted", "task_id": other_id}
    remaining_ids = {task["id"] for task in client.get("/tasks").json()}
    assert other_id not in remaining_ids


def test_two_conversation_ids_stay_isolated_for_confirmation(client):
    created_a = _execute(client, "Add a task to buy milk")
    task_a = created_a["result"]["id"]
    asked_a = _execute(client, f"Delete task {task_a}", conversation_id=created_a["conversation_id"])
    conversation_a = asked_a["conversation_id"]

    created_b = _execute(client, "Add a task to walk the dog")
    task_b = created_b["result"]["id"]
    asked_b = _execute(client, f"Delete task {task_b}", conversation_id=created_b["conversation_id"])
    conversation_b = asked_b["conversation_id"]

    # Cancel B first, confirm A - they must not interfere with each other.
    cancelled_b = _execute(client, "cancel", conversation_id=conversation_b)
    assert cancelled_b["result"] is None

    confirmed_a = _execute(client, "yes", conversation_id=conversation_a)
    assert confirmed_a["result"] == {"status": "deleted", "task_id": task_a}

    remaining_ids = {task["id"] for task in client.get("/tasks").json()}
    assert task_a not in remaining_ids
    assert task_b in remaining_ids


def test_confirmation_state_clears_after_success(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    asked = _execute(client, f"Delete task {task_id}")
    conversation_id = asked["conversation_id"]
    _execute(client, "yes", conversation_id=conversation_id)

    # A later, unrelated message on the same conversation_id must go
    # through the normal provider flow, not be treated as another
    # confirmation reply.
    followup = _execute(client, "Show me all tasks", conversation_id=conversation_id)

    assert followup["selected_tool"] == "list_tasks"
    assert followup["needs_confirmation"] is False


def test_non_destructive_tools_still_execute_immediately(client, monkeypatch):
    from app.services import weather_service

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

    create_data = _execute(client, "Add a task to buy milk")
    task_id = create_data["result"]["id"]
    assert create_data["needs_confirmation"] is False

    list_data = _execute(client, "Show me all tasks")
    assert list_data["needs_confirmation"] is False

    weather_data = _execute(client, "What is the weather in London?")
    assert weather_data["needs_confirmation"] is False
    assert weather_data["result"]["city"] == "London"

    update_data = _execute(client, f"Update task {task_id} to Buy oat milk")
    assert update_data["needs_confirmation"] is False
    assert update_data["result"]["title"] == "Buy oat milk"

    done_data = _execute(client, f"Mark task {task_id} as done")
    assert done_data["needs_confirmation"] is False
    assert done_data["result"]["done"] is True

    # None of the above should have left a pending confirmation behind.
    from app.services import conversation_memory

    assert conversation_memory._pending_confirmation == {}
