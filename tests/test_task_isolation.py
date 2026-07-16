"""Cross-user task ownership isolation tests.

Every task query/update/completion/deletion is scoped to the
authenticated user (see app/services/task_service.py). A task that
exists but belongs to a different user must be indistinguishable from
one that doesn't exist at all: always a 404, never a 403, and never any
observable difference from a genuinely unknown id (requirements #7-9).

`client` is authenticated as "alice"; `other_user_headers` (from
tests/conftest.py) authenticates as "bob" - a second, distinct user.
"""


def test_list_tasks_only_returns_own_tasks(client, other_user_headers):
    client.post("/tasks", json={"title": "Alice task 1"})
    client.post("/tasks", json={"title": "Alice task 2"})
    client.post("/tasks", json={"title": "Bob task"}, headers=other_user_headers)

    alice_tasks = client.get("/tasks").json()
    assert {task["title"] for task in alice_tasks} == {"Alice task 1", "Alice task 2"}

    bob_tasks = client.get("/tasks", headers=other_user_headers).json()
    assert [task["title"] for task in bob_tasks] == ["Bob task"]


def test_cross_user_get_task_returns_404_with_no_existence_leak(client, other_user_headers):
    created = client.post("/tasks", json={"title": "Alice task"}).json()

    cross_user = client.get(f"/tasks/{created['id']}", headers=other_user_headers)
    nonexistent = client.get("/tasks/999999", headers=other_user_headers)

    assert cross_user.status_code == 404
    assert nonexistent.status_code == 404
    # Identical response shape for "exists but isn't yours" and "doesn't
    # exist at all" - bob can't learn alice's task id is real.
    assert cross_user.json() == nonexistent.json()


def test_cross_user_update_task_returns_404_and_does_not_modify(client, other_user_headers):
    created = client.post("/tasks", json={"title": "Alice task"}).json()

    response = client.patch(f"/tasks/{created['id']}", json={"title": "Hacked by bob"}, headers=other_user_headers)
    assert response.status_code == 404

    refetched = client.get(f"/tasks/{created['id']}").json()
    assert refetched["title"] == "Alice task"


def test_cross_user_mark_done_returns_404_and_does_not_modify(client, other_user_headers):
    created = client.post("/tasks", json={"title": "Alice task"}).json()

    response = client.patch(f"/tasks/{created['id']}/done", headers=other_user_headers)
    assert response.status_code == 404

    refetched = client.get(f"/tasks/{created['id']}").json()
    assert refetched["done"] is False


def test_cross_user_delete_task_returns_404_and_task_survives(client, other_user_headers):
    created = client.post("/tasks", json={"title": "Alice task"}).json()

    response = client.delete(f"/tasks/{created['id']}", headers=other_user_headers)
    assert response.status_code == 404

    assert client.get(f"/tasks/{created['id']}").status_code == 200


def test_own_user_full_crud_lifecycle_still_works(client):
    """Regression guard: ownership filtering must not break same-user CRUD."""
    created = client.post("/tasks", json={"title": "Alice task"}).json()
    task_id = created["id"]

    assert client.get(f"/tasks/{task_id}").status_code == 200
    assert client.patch(f"/tasks/{task_id}", json={"title": "Updated"}).status_code == 200
    assert client.patch(f"/tasks/{task_id}/done").json()["done"] is True
    assert client.delete(f"/tasks/{task_id}").status_code == 204
    assert client.get(f"/tasks/{task_id}").status_code == 404
