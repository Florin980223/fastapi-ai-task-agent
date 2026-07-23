"""Tests proving tasks are actually persisted in SQLite.

These hit the REST endpoints directly (unlike most of the rest of the
suite, which exercises task storage indirectly through POST
/agent/execute) and, where noted, re-read data through a brand-new
SQLAlchemy Session (the new_db_session fixture from conftest.py) to
prove it was really committed to the database rather than just held
in a Python object.
"""

from app.services import task_service


def test_create_task_writes_to_sqlite(client, new_db_session, test_user_id):
    response = client.post("/tasks", json={"title": "Buy milk", "description": "2 liters"})

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Buy milk"
    assert body["description"] == "2 liters"
    assert body["done"] is False

    # Re-fetch through a brand-new session, independent of whatever
    # session the request above used, to prove the row is really in
    # the database and not just an in-memory Python object.
    found = task_service.find_task(new_db_session, test_user_id, body["id"])
    assert found is not None
    assert found.title == "Buy milk"
    assert found.description == "2 liters"


def test_list_tasks_reads_persisted_tasks(client):
    client.post("/tasks", json={"title": "Buy milk"})
    client.post("/tasks", json={"title": "Walk the dog"})

    response = client.get("/tasks")

    assert response.status_code == 200
    titles = {task["title"] for task in response.json()}
    assert titles == {"Buy milk", "Walk the dog"}


def test_update_task_persists_changes(client):
    created = client.post("/tasks", json={"title": "Buy milk"}).json()
    task_id = created["id"]

    response = client.patch(f"/tasks/{task_id}", json={"title": "Buy oat milk"})
    assert response.status_code == 200
    assert response.json()["title"] == "Buy oat milk"

    # Verify via a separate request, proving the change is durable
    # rather than just returned by the mutating call.
    refetched = client.get(f"/tasks/{task_id}").json()
    assert refetched["title"] == "Buy oat milk"


def test_mark_task_done_persists_done_true(client):
    created = client.post("/tasks", json={"title": "Buy milk"}).json()
    task_id = created["id"]

    response = client.patch(f"/tasks/{task_id}/done")
    assert response.status_code == 200
    assert response.json()["done"] is True

    refetched = client.get(f"/tasks/{task_id}").json()
    assert refetched["done"] is True


def test_delete_task_removes_the_database_record(client, new_db_session, test_user_id):
    created = client.post("/tasks", json={"title": "Buy milk"}).json()
    task_id = created["id"]

    response = client.delete(f"/tasks/{task_id}")
    assert response.status_code == 204

    assert client.get(f"/tasks/{task_id}").status_code == 404

    assert task_service.find_task(new_db_session, test_user_id, task_id) is None


def test_done_filtering_reflects_persisted_state(client):
    pending = client.post("/tasks", json={"title": "Pending task"}).json()
    done = client.post("/tasks", json={"title": "Done task"}).json()
    client.patch(f"/tasks/{done['id']}/done")

    all_tasks = client.get("/tasks").json()
    assert {task["id"] for task in all_tasks} == {pending["id"], done["id"]}

    done_tasks = client.get("/tasks", params={"done": True}).json()
    assert [task["id"] for task in done_tasks] == [done["id"]]

    pending_tasks = client.get("/tasks", params={"done": False}).json()
    assert [task["id"] for task in pending_tasks] == [pending["id"]]


def test_task_remains_available_through_a_new_repository_session(client, new_db_session, test_user_id):
    created = client.post("/tasks", json={"title": "Buy milk"}).json()

    tasks = task_service.list_tasks(new_db_session, test_user_id)
    assert [task.id for task in tasks] == [created["id"]]


# --- Priority and due_date ------------------------------------------------


def test_create_task_defaults_priority_to_medium_and_due_date_to_none(client):
    response = client.post("/tasks", json={"title": "Buy milk"})

    assert response.status_code == 201
    body = response.json()
    assert body["priority"] == "medium"
    assert body["due_date"] is None


def test_create_task_accepts_explicit_priority_and_due_date(client):
    response = client.post(
        "/tasks", json={"title": "Prepare Q3 report", "priority": "high", "due_date": "2026-08-15"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["priority"] == "high"
    assert body["due_date"] == "2026-08-15"


def test_create_task_rejects_invalid_priority(client):
    response = client.post("/tasks", json={"title": "Buy milk", "priority": "urgent"})

    assert response.status_code == 422


def test_create_task_rejects_malformed_due_date(client):
    response = client.post("/tasks", json={"title": "Buy milk", "due_date": "15/08/2026"})

    assert response.status_code == 422


def test_update_omitted_priority_leaves_it_unchanged(client):
    created = client.post("/tasks", json={"title": "Buy milk", "priority": "high"}).json()
    task_id = created["id"]

    response = client.patch(f"/tasks/{task_id}", json={"title": "Buy oat milk"})

    assert response.status_code == 200
    assert response.json()["priority"] == "high"


def test_update_explicit_valid_priority_updates_it(client):
    created = client.post("/tasks", json={"title": "Buy milk"}).json()
    task_id = created["id"]
    assert created["priority"] == "medium"

    response = client.patch(f"/tasks/{task_id}", json={"priority": "low"})

    assert response.status_code == 200
    assert response.json()["priority"] == "low"
    assert client.get(f"/tasks/{task_id}").json()["priority"] == "low"


def test_update_explicit_null_priority_returns_422(client):
    created = client.post("/tasks", json={"title": "Buy milk", "priority": "high"}).json()
    task_id = created["id"]

    response = client.patch(f"/tasks/{task_id}", json={"priority": None})

    assert response.status_code == 422
    # Never silently treated as "leave unchanged" - the priority is untouched.
    assert client.get(f"/tasks/{task_id}").json()["priority"] == "high"


def test_update_unsupported_priority_string_returns_422(client):
    created = client.post("/tasks", json={"title": "Buy milk"}).json()
    task_id = created["id"]

    response = client.patch(f"/tasks/{task_id}", json={"priority": "urgent"})

    assert response.status_code == 422
    assert client.get(f"/tasks/{task_id}").json()["priority"] == "medium"


def test_update_omitted_due_date_leaves_it_unchanged(client):
    created = client.post("/tasks", json={"title": "Buy milk", "due_date": "2026-08-15"}).json()
    task_id = created["id"]

    response = client.patch(f"/tasks/{task_id}", json={"title": "Buy oat milk"})

    assert response.status_code == 200
    assert response.json()["due_date"] == "2026-08-15"


def test_update_sets_due_date(client):
    created = client.post("/tasks", json={"title": "Buy milk"}).json()
    task_id = created["id"]
    assert created["due_date"] is None

    response = client.patch(f"/tasks/{task_id}", json={"due_date": "2026-09-01"})

    assert response.status_code == 200
    assert response.json()["due_date"] == "2026-09-01"


def test_update_changes_due_date(client):
    created = client.post("/tasks", json={"title": "Buy milk", "due_date": "2026-08-15"}).json()
    task_id = created["id"]

    response = client.patch(f"/tasks/{task_id}", json={"due_date": "2026-09-01"})

    assert response.status_code == 200
    assert response.json()["due_date"] == "2026-09-01"


def test_update_explicit_null_due_date_clears_it(client):
    created = client.post("/tasks", json={"title": "Buy milk", "due_date": "2026-08-15"}).json()
    task_id = created["id"]

    response = client.patch(f"/tasks/{task_id}", json={"due_date": None})

    assert response.status_code == 200
    assert response.json()["due_date"] is None
    assert client.get(f"/tasks/{task_id}").json()["due_date"] is None


def test_update_rejects_malformed_due_date(client):
    created = client.post("/tasks", json={"title": "Buy milk", "due_date": "2026-08-15"}).json()
    task_id = created["id"]

    response = client.patch(f"/tasks/{task_id}", json={"due_date": "not-a-date"})

    assert response.status_code == 422
    # Untouched by the rejected request.
    assert client.get(f"/tasks/{task_id}").json()["due_date"] == "2026-08-15"


def test_agent_execute_create_task_result_reflects_default_priority_and_due_date(client):
    created = client.post("/agent/execute", json={"message": "Add a task to buy milk"}).json()

    assert created["result"]["priority"] == "medium"
    assert created["result"]["due_date"] is None


def test_agent_execute_endpoints_work_with_sqlite(client):
    created = client.post("/agent/execute", json={"message": "Add a task to buy milk"}).json()
    assert created["selected_tool"] == "create_task"
    task_id = created["result"]["id"]

    listed = client.post("/agent/execute", json={"message": "Show me all tasks"}).json()
    assert listed["selected_tool"] == "list_tasks"
    assert len(listed["result"]) == 1

    done = client.post("/agent/execute", json={"message": f"Mark task {task_id} as done"}).json()
    assert done["selected_tool"] == "mark_task_done"
    assert done["result"]["done"] is True

    # Confirm the agent's mutation is durable, not just reflected in
    # the response - re-check via a plain REST GET.
    assert client.get(f"/tasks/{task_id}").json()["done"] is True

    asked = client.post("/agent/execute", json={"message": f"Delete task {task_id}"}).json()
    assert asked["selected_tool"] == "delete_task"
    assert asked["needs_confirmation"] is True

    deleted = client.post(
        "/agent/execute",
        json={"message": "yes", "conversation_id": asked["conversation_id"]},
    ).json()
    assert deleted["result"] == {"status": "deleted", "task_id": task_id}
    assert client.get(f"/tasks/{task_id}").status_code == 404
