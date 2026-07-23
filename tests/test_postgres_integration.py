"""Opt-in PostgreSQL integration test.

Skipped entirely unless POSTGRES_TEST_DATABASE_URL is set - the rest of
the suite (see tests/conftest.py) always runs against an isolated SQLite
:memory: database and never imports this file's fixtures. See README.md's
"PostgreSQL (optional)" section for how to stand up a local Postgres and
run this file against it.

Several layered guards on POSTGRES_TEST_DATABASE_URL keep this file from
ever running against a real/shared database by accident:
  1. Not set at all -> skip (pytestmark below).
  2. Host must be localhost/127.0.0.1 - never a remote server.
  3. Database name must end in "_test".
  4. Database name must not be the real app database name ("taskagent").
  5. Must not be equal to the app's own DATABASE_URL (app.config.DATABASE_URL).
  6. Never opens tasks.db or touches app.database's module-level engine/
     SessionLocal - this file builds its own, completely separate engine.

No test here ever issues DROP or TRUNCATE: `command.upgrade(cfg, "head")`
is idempotent (safe to rerun against an already-migrated database), and
each test deletes only the specific row(s) it inserted, by id.
"""

import os

import pytest
from alembic import command
from alembic.config import Config
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import DATABASE_URL as APP_DATABASE_URL
from app.db_models import Task

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
_ALEMBIC_DIR = _REPO_ROOT / "alembic"

RAW_TEST_URL = os.environ.get("POSTGRES_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not RAW_TEST_URL, reason="POSTGRES_TEST_DATABASE_URL not set - PostgreSQL integration test skipped"
)


def _validated_url(raw: str):
    """Refuses anything that isn't an obviously-scratch local test database."""
    from sqlalchemy.engine import make_url

    url = make_url(raw)
    if url.host not in ("localhost", "127.0.0.1"):
        pytest.fail(f"POSTGRES_TEST_DATABASE_URL host must be localhost/127.0.0.1, got {url.host!r}")
    database = url.database or ""
    if not database.endswith("_test"):
        pytest.fail(f"POSTGRES_TEST_DATABASE_URL database name must end in '_test', got {database!r}")
    if database == "taskagent":
        pytest.fail("Refusing to run against the real 'taskagent' database")
    if raw == APP_DATABASE_URL:
        pytest.fail("POSTGRES_TEST_DATABASE_URL must not equal the app's own DATABASE_URL")
    return url


@pytest.fixture(scope="module")
def postgres_test_url():
    _validated_url(RAW_TEST_URL)
    return RAW_TEST_URL


@pytest.fixture(scope="module")
def migrated_engine(postgres_test_url):
    """Brings the scratch database to the current Alembic head (idempotent
    - safe even if it's already there from a previous run) and hands back
    a dedicated engine, completely independent of app.database.engine.
    """
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", postgres_test_url)
    command.upgrade(cfg, "head")
    command.check(cfg)

    engine = create_engine(postgres_test_url, pool_pre_ping=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def session(migrated_engine):
    session_local = sessionmaker(bind=migrated_engine, autoflush=False, autocommit=False)
    db = session_local()
    try:
        yield db
    finally:
        db.close()


def test_alembic_upgrade_head_reaches_baseline(migrated_engine):
    from alembic.runtime.migration import MigrationContext

    with migrated_engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    assert current == "0002_add_task_priority_and_due_date"


def test_task_roundtrip_against_real_postgresql(session):
    task = Task(user_id="pg-integration-test", title="Buy milk", description=None, done=False)
    session.add(task)
    session.commit()
    task_id = task.id

    try:
        fetched = session.get(Task, task_id)
        assert fetched is not None
        assert fetched.title == "Buy milk"
        assert fetched.user_id == "pg-integration-test"
        assert fetched.done is False
    finally:
        # Deletes only the single row this test inserted - never a DROP/TRUNCATE.
        session.delete(session.get(Task, task_id))
        session.commit()


def test_task_update_persists_against_real_postgresql(session):
    task = Task(user_id="pg-integration-test", title="Buy eggs", description="a dozen", done=False)
    session.add(task)
    session.commit()
    task_id = task.id

    try:
        task.done = True
        session.commit()

        session.expire_all()
        refetched = session.get(Task, task_id)
        assert refetched.done is True
        assert refetched.description == "a dozen"
    finally:
        session.delete(session.get(Task, task_id))
        session.commit()
