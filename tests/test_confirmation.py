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


def test_confirmation_reply_content_can_never_redirect_what_executes(client):
    """The tool/arguments that execute on confirmation come only from the
    atomically-consumed stored row (conversation_memory.consume_confirmation)
    - never from anything in the confirming message's own text. A reply
    that isn't an exact "yes"/"confirm"/"proceed"/"da" match is treated as
    ambiguous and changes nothing, even if it looks like an attempt to
    redirect the action to a different task.
    """
    task_a = _execute(client, "Add a task to buy milk")["result"]["id"]
    task_b = _execute(client, "Add a task to buy eggs")["result"]["id"]

    asked = _execute(client, f"Delete task {task_a}")
    conversation_id = asked["conversation_id"]
    assert asked["needs_confirmation"] is True

    # Not an exact confirmation phrase - must be treated as ambiguous, not
    # as a "yes" that somehow also changed which task is targeted.
    ambiguous = _execute(client, f"yes but delete task {task_b} instead", conversation_id=conversation_id)

    assert ambiguous["needs_confirmation"] is True
    assert ambiguous["confirmation_question"] == asked["confirmation_question"]
    remaining_ids = {task["id"] for task in client.get("/tasks").json()}
    assert remaining_ids == {task_a, task_b}

    # The original pending confirmation (for task_a) is still exactly
    # what a real "yes" executes.
    confirmed = _execute(client, "yes", conversation_id=conversation_id)
    assert confirmed["result"] == {"status": "deleted", "task_id": task_a}
    assert {task["id"] for task in client.get("/tasks").json()} == {task_b}


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


def test_pending_confirmation_is_isolated_between_users(client, other_user_headers):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    asked = _execute(client, f"Delete task {task_id}")
    conversation_id = asked["conversation_id"]
    assert asked["needs_confirmation"] is True

    # Bob reuses alice's conversation_id and says "yes". He has no
    # pending confirmation under (bob, conversation_id), so nothing of
    # alice's is deleted - his "yes" is just an ordinary, unmatched
    # message.
    bob_response = client.post(
        "/agent/execute",
        json={"message": "yes", "conversation_id": conversation_id},
        headers=other_user_headers,
    )
    assert bob_response.status_code == 200
    bob_data = bob_response.json()
    assert bob_data["result"] is None
    assert bob_data["needs_confirmation"] is False
    assert any(task["id"] == task_id for task in client.get("/tasks").json())

    # Alice's own later "yes" still executes her own pending delete.
    confirmed = _execute(client, "yes", conversation_id=conversation_id)
    assert confirmed["result"] == {"status": "deleted", "task_id": task_id}
    assert client.get("/tasks").json() == []


def test_non_destructive_tools_still_execute_immediately(client, monkeypatch, new_db_session, test_user_id):
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

    # None of the above should have left a pending confirmation behind,
    # for this user, in the durable ConversationState table.
    from app.db_models import ConversationState

    rows = new_db_session.query(ConversationState).filter_by(user_id=test_user_id).all()
    assert all(row.confirmation_tool is None for row in rows)


def test_confirmation_survives_restart(restart_client_factory):
    with restart_client_factory() as client:
        created = _execute(client, "Add a task to buy milk")
        task_id = created["result"]["id"]
        asked = _execute(client, f"Delete task {task_id}")
        conversation_id = asked["conversation_id"]
        assert asked["needs_confirmation"] is True

    # A brand new engine/Session/TestClient - simulating a fresh process
    # against the same on-disk database - must still see the pending
    # confirmation parked above and execute it.
    with restart_client_factory() as client:
        confirmed = _execute(client, "yes", conversation_id=conversation_id)
        assert confirmed["result"] == {"status": "deleted", "task_id": task_id}
        assert client.get("/tasks").json() == []


def test_second_yes_after_confirmation_executes_nothing(client):
    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]
    asked = _execute(client, f"Delete task {task_id}")
    conversation_id = asked["conversation_id"]

    first = _execute(client, "yes", conversation_id=conversation_id)
    assert first["result"] == {"status": "deleted", "task_id": task_id}

    # The confirmation was consumed by the first "yes" - a second one on
    # the same conversation must find nothing pending and must not
    # attempt delete_task again (it already doesn't exist).
    second = _execute(client, "yes", conversation_id=conversation_id)
    assert second["needs_confirmation"] is False
    assert second["selected_tool"] != "delete_task"


def test_concurrent_consume_confirmation_only_succeeds_once(client, new_db_session, test_user_id):
    import app.database as database
    from uuid import UUID

    from app.services import conversation_memory

    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]
    asked = _execute(client, f"Delete task {task_id}")
    conversation_id = UUID(asked["conversation_id"])

    # Two independent Sessions, standing in for two concurrent HTTP
    # requests each with their own request-scoped db - racing to consume
    # the same pending confirmation.
    second_session = database.SessionLocal()
    try:
        first = conversation_memory.consume_confirmation(new_db_session, test_user_id, conversation_id)
        second = conversation_memory.consume_confirmation(second_session, test_user_id, conversation_id)
    finally:
        second_session.close()

    assert first is not None
    assert first.selected_tool == "delete_task"
    assert second is None


def test_expired_confirmation_cannot_delete(client, new_db_session, test_user_id):
    from datetime import datetime, timedelta, timezone
    from uuid import UUID

    from app.db_models import ConversationState

    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]
    asked = _execute(client, f"Delete task {task_id}")
    conversation_id = UUID(asked["conversation_id"])

    row = (
        new_db_session.query(ConversationState)
        .filter_by(user_id=test_user_id, conversation_id=conversation_id)
        .one()
    )
    row.confirmation_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    new_db_session.commit()

    confirmed = _execute(client, "yes", conversation_id=str(conversation_id))
    assert confirmed["needs_confirmation"] is False
    assert confirmed["selected_tool"] != "delete_task"
    assert any(task["id"] == task_id for task in client.get("/tasks").json())


def test_confirmation_persistence_failure_returns_safe_error(client, monkeypatch):
    from app.services import conversation_memory

    def boom(*args, **kwargs):
        raise RuntimeError("db explosion containing a fake secret sauce")

    monkeypatch.setattr(conversation_memory, "set_confirmation", boom)

    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]

    response = client.post("/agent/execute", json={"message": f"Delete task {task_id}"})

    assert response.status_code == 500
    body = response.json()
    assert "secret sauce" not in str(body)
    assert body["detail"] == "Failed to save conversation state. Please try again."
    # Nothing was queued - the task is untouched.
    assert any(task["id"] == task_id for task in client.get("/tasks").json())


def test_raw_api_key_never_appears_in_conversation_state(client, new_db_session, test_api_key):
    from app.db_models import ConversationState

    created = _execute(client, "Add a task to buy milk")
    task_id = created["result"]["id"]
    asked = _execute(client, "Delete a task")
    conversation_id = asked["conversation_id"]
    _execute(client, str(task_id), conversation_id=conversation_id)

    rows = new_db_session.query(ConversationState).all()
    assert rows
    for row in rows:
        for column in ConversationState.__table__.columns:
            value = getattr(row, column.name)
            assert test_api_key not in str(value)
