"""Tests proving tasks are actually persisted in SQLite.

These hit the REST endpoints directly (unlike most of the rest of the
suite, which exercises task storage indirectly through POST
/agent/execute) and, where noted, re-read data through a brand-new
SQLAlchemy Session (the new_db_session fixture from conftest.py) to
prove it was really committed to the database rather than just held
in a Python object.
"""

from app.services import task_service


def test_create_task_writes_to_sqlite(client, new_db_session):
    response = client.post("/tasks", json={"title": "Buy milk", "description": "2 liters"})

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Buy milk"
    assert body["description"] == "2 liters"
    assert body["done"] is False

    # Re-fetch through a brand-new session, independent of whatever
    # session the request above used, to prove the row is really in
    # the database and not just an in-memory Python object.
    found = task_service.find_task(new_db_session, body["id"])
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


def test_delete_task_removes_the_database_record(client, new_db_session):
    created = client.post("/tasks", json={"title": "Buy milk"}).json()
    task_id = created["id"]

    response = client.delete(f"/tasks/{task_id}")
    assert response.status_code == 204

    assert client.get(f"/tasks/{task_id}").status_code == 404

    assert task_service.find_task(new_db_session, task_id) is None


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


def test_task_remains_available_through_a_new_repository_session(client, new_db_session):
    created = client.post("/tasks", json={"title": "Buy milk"}).json()

    tasks = task_service.list_tasks(new_db_session)
    assert [task.id for task in tasks] == [created["id"]]


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

    deleted = client.post("/agent/execute", json={"message": f"Delete task {task_id}"}).json()
    assert deleted["selected_tool"] == "delete_task"
    assert deleted["result"] == {"status": "deleted", "task_id": task_id}
    assert client.get(f"/tasks/{task_id}").status_code == 404
