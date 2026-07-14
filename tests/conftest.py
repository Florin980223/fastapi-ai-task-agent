"""Shared pytest fixtures for the test suite."""

import os

# Make sure the production SQLite file is never touched, even by the
# app's own startup (lifespan) hook - set this before app.main (and
# anything that imports app.config) is imported below.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.conversation_memory as conversation_memory
from app.database import Base, get_db
from app.main import app

# A separate in-memory SQLite database dedicated to tests. StaticPool
# forces SQLAlchemy to reuse a single connection for the whole engine -
# without it, each connection checkout would get its own *empty*
# in-memory database, since TestClient issues requests on a different
# thread than the one that created the engine.
test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)


def _override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = _override_get_db


@pytest.fixture
def client():
    """A FastAPI TestClient for making requests against the app."""
    return TestClient(app)


@pytest.fixture
def new_db_session():
    """A brand-new Session against the test database.

    Bound to the same test_engine the app's overridden get_db()
    dependency uses, but a fresh Session object - handy for proving
    data survives independently of whatever session an HTTP request
    used. Not imported directly by test modules (that would re-import
    this file as a second, distinct "tests.conftest" module, since
    there's no tests/__init__.py) - go through this fixture instead.
    """
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def reset_tasks_db():
    """Reset the SQLite task store before and after every test.

    Dropping and recreating the tables gives each test an empty
    database and resets the autoincrement id sequence, replacing the
    old tasks_db.clear() / _next_id reset used by the in-memory store.
    """
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture(autouse=True)
def reset_conversation_memory():
    """Reset pending-clarification and remembered-context memory before
    and after every test.

    conversation_memory._pending and _last_task_id are module-level
    globals, same reason as reset_tasks_db above.
    """
    conversation_memory._pending.clear()
    conversation_memory._last_task_id.clear()
    yield
    conversation_memory._pending.clear()
    conversation_memory._last_task_id.clear()
