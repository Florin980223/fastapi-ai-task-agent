"""Tests for the per-user, in-memory, fixed-window rate limiter
(app/services/rate_limiter.py) on POST /agent/execute.

Disabled by default for the rest of the test suite (see
tests/conftest.py's RATE_LIMIT_ENABLED=false override, set before
app.main is imported) - every test in this file explicitly re-enables
it via monkeypatching app.config, which the limiter reads live, and
relies on tests/conftest.py's autouse reset_rate_limiter fixture to
start from a clean counter state.
"""

import logging

import app.config as config
from app.services import rate_limiter


def _execute(client, message="Show me all tasks"):
    return client.post("/agent/execute", json={"message": message})


def test_default_test_suite_is_unaffected_by_rate_limiting():
    # tests/conftest.py sets this before app.main is even imported -
    # every other test in the suite runs with it off, regardless of how
    # many requests it makes.
    assert config.RATE_LIMIT_ENABLED is False


def test_requests_within_the_limit_succeed(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 3)
    monkeypatch.setattr(config, "RATE_LIMIT_WINDOW_SECONDS", 60)

    for _ in range(3):
        assert _execute(client).status_code == 200


def test_exceeding_the_limit_returns_429_with_retry_after(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 2)
    monkeypatch.setattr(config, "RATE_LIMIT_WINDOW_SECONDS", 60)

    assert _execute(client).status_code == 200
    assert _execute(client).status_code == 200
    response = _execute(client)

    assert response.status_code == 429
    retry_after = int(response.headers["Retry-After"])
    assert retry_after >= 1
    assert response.json()["detail"]


def test_limit_is_isolated_per_user(client, other_user_headers, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 1)
    monkeypatch.setattr(config, "RATE_LIMIT_WINDOW_SECONDS", 60)

    assert _execute(client).status_code == 200
    assert _execute(client).status_code == 429

    other_response = client.post(
        "/agent/execute", json={"message": "Show me all tasks"}, headers=other_user_headers
    )
    assert other_response.status_code == 200


def test_window_reset_allows_requests_again(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 1)
    monkeypatch.setattr(config, "RATE_LIMIT_WINDOW_SECONDS", 60)

    fake_now = [1_000.0]
    monkeypatch.setattr(rate_limiter, "_monotonic", lambda: fake_now[0])

    assert _execute(client).status_code == 200
    assert _execute(client).status_code == 429

    fake_now[0] += 61  # past the 60s window
    assert _execute(client).status_code == 200


def test_rate_limiting_disabled_never_returns_429(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 1)

    for _ in range(5):
        assert _execute(client).status_code == 200


def test_dependency_returns_the_authenticated_user(client, test_user_id, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 10)

    # If enforce_execute_rate_limit didn't return the AuthenticatedUser
    # unchanged, current_user.user_id inside execute() would be broken,
    # and per-user isolation (task ownership, conversation state) would
    # break with it - a normal, fully successful request is proof it's
    # wired through correctly.
    response = _execute(client, "Add a task to buy milk")
    assert response.status_code == 200
    assert response.json()["result"]["title"] == "buy milk"


def test_raw_api_key_never_appears_in_limiter_state_or_logs(client, test_api_key, caplog, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "RATE_LIMIT_REQUESTS", 1)
    monkeypatch.setattr(config, "RATE_LIMIT_WINDOW_SECONDS", 60)

    with caplog.at_level(logging.DEBUG):
        _execute(client)
        _execute(client)  # triggers a 429

    assert all(test_api_key != key for key in rate_limiter._counters)
    for record in caplog.records:
        assert test_api_key not in record.getMessage()
