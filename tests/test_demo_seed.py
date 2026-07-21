"""Unit tests for docker/demo_seed.py's pure guard/idempotency logic.

Loaded by file path (rather than `import docker.demo_seed`), same
reasoning as tests/test_docker_healthcheck.py: docker/ is a plain
scripts directory, not a Python package.

These tests cover only the pure guard function (_validate_target) and
the idempotency/dry-run-writes-nothing behavior of the SQL helpers
against a throwaway SQLite table standing in for the real `tasks`
table shape - fast and fully offline. They do not replace the real
guarded seed flow being exercised once against the actual isolated
demo PostgreSQL database during implementation (see docs/LOCAL_DEMO.md).
"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

_MODULE_PATH = Path(__file__).resolve().parent.parent / "docker" / "demo_seed.py"
_spec = importlib.util.spec_from_file_location("docker_demo_seed", _MODULE_PATH)
demo_seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo_seed)


VALID_URL = "postgresql+psycopg://taskagent_demo:pw@localhost/taskagent_demo"


def test_validate_target_accepts_the_exact_demo_target():
    demo_seed._validate_target(VALID_URL, "demo_user_a")
    demo_seed._validate_target(VALID_URL, "demo_user_b")


def test_validate_target_refuses_non_postgres_scheme():
    with pytest.raises(demo_seed.DemoSeedGuardError, match="scheme"):
        demo_seed._validate_target("sqlite:///./tasks.db", "demo_user_a")


def test_validate_target_refuses_wrong_host():
    with pytest.raises(demo_seed.DemoSeedGuardError, match="host"):
        demo_seed._validate_target(
            "postgresql+psycopg://taskagent_demo:pw@example.com/taskagent_demo", "demo_user_a"
        )


def test_validate_target_refuses_wrong_database_name():
    with pytest.raises(demo_seed.DemoSeedGuardError, match="database name"):
        demo_seed._validate_target("postgresql+psycopg://taskagent:pw@localhost/taskagent", "demo_user_a")


@pytest.mark.parametrize("bad_user_id", ["florin", "alice", "ci-user", "ci_user", "smoke_test_user", "demo_user_c"])
def test_validate_target_refuses_non_allowlisted_user_id(bad_user_id):
    with pytest.raises(demo_seed.DemoSeedGuardError, match="user_id"):
        demo_seed._validate_target(VALID_URL, bad_user_id)


def test_validate_target_refuses_url_equal_to_process_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", VALID_URL)
    with pytest.raises(demo_seed.DemoSeedGuardError, match="DATABASE_URL"):
        demo_seed._validate_target(VALID_URL, "demo_user_a")


def test_validate_target_allows_when_process_database_url_differs(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./tasks.db")
    demo_seed._validate_target(VALID_URL, "demo_user_a")


@pytest.fixture
def tasks_engine():
    """A throwaway SQLite engine with a `tasks` table matching the real
    schema's relevant columns - stands in for the real Postgres `tasks`
    table so the SQL helper logic (not the guard, which requires a real
    postgresql:// URL) can be tested fast and offline.
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE tasks ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "user_id TEXT NOT NULL, "
                "title TEXT NOT NULL, "
                "description TEXT, "
                "done BOOLEAN NOT NULL DEFAULT 0)"
            )
        )
    yield engine
    engine.dispose()


def test_missing_titles_reports_all_when_none_exist(tasks_engine):
    with tasks_engine.connect() as connection:
        missing = demo_seed._missing_titles(connection, "demo_user_a")
    assert missing == demo_seed._SEED_TITLES


def test_insert_missing_is_idempotent_and_never_duplicates(tasks_engine):
    with tasks_engine.begin() as connection:
        missing = demo_seed._missing_titles(connection, "demo_user_a")
        demo_seed._insert_missing(connection, "demo_user_a", missing)

    with tasks_engine.connect() as connection:
        after_first = demo_seed._missing_titles(connection, "demo_user_a")
    assert after_first == []

    # Second run: nothing missing, nothing inserted.
    with tasks_engine.begin() as connection:
        missing_again = demo_seed._missing_titles(connection, "demo_user_a")
        demo_seed._insert_missing(connection, "demo_user_a", missing_again)
    assert missing_again == []

    with tasks_engine.connect() as connection:
        count = connection.execute(
            text("SELECT COUNT(*) FROM tasks WHERE user_id = :user_id"), {"user_id": "demo_user_a"}
        ).scalar_one()
    assert count == len(demo_seed._SEED_TITLES)


def test_insert_missing_never_touches_a_different_users_rows(tasks_engine):
    with tasks_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tasks (user_id, title, description, done) VALUES (:user_id, :title, NULL, 0)"),
            {"user_id": "demo_user_b", "title": "Pre-existing task for demo_user_b"},
        )

    with tasks_engine.begin() as connection:
        missing = demo_seed._missing_titles(connection, "demo_user_a")
        demo_seed._insert_missing(connection, "demo_user_a", missing)

    with tasks_engine.connect() as connection:
        b_titles = connection.execute(
            text("SELECT title FROM tasks WHERE user_id = :user_id"), {"user_id": "demo_user_b"}
        ).scalars().all()
    assert b_titles == ["Pre-existing task for demo_user_b"]
