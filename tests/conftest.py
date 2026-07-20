"""Shared pytest fixtures for the test suite."""

import os
from contextlib import contextmanager

# Make sure the production SQLite file is never touched, even by the
# app's own startup (lifespan) hook - set this before app.main (and
# anything that imports app.config) is imported below.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

# Deterministic test API keys - two distinct users, so cross-user
# isolation tests never need real/secret configuration. Set before
# app.main (and anything that imports app.config) is imported below,
# since app.config.API_KEYS is parsed once, eagerly, at import time.
TEST_API_KEY = "test-key-alice"
TEST_USER_ID = "alice"
OTHER_TEST_API_KEY = "test-key-bob"
OTHER_TEST_USER_ID = "bob"
os.environ["API_KEYS"] = f"{TEST_API_KEY}:{TEST_USER_ID},{OTHER_TEST_API_KEY}:{OTHER_TEST_USER_ID}"

# The rate limiter (app/services/rate_limiter.py) is disabled by default
# for the whole test suite - the same unconditional-override pattern as
# DATABASE_URL/API_KEYS above - so none of the other tests need to know
# it exists. tests/test_rate_limit.py re-enables it locally via
# monkeypatching app.config directly, read live by the limiter.
os.environ["RATE_LIMIT_ENABLED"] = "false"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.database as database
from app.database import Base, get_db
from app.main import app
from app.services import schema_migration

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

# agent_trace_service deliberately opens its own database.SessionLocal()
# session (never the request's injected db) so a tracing failure can
# never touch the request's own transaction. Rebinding the module-level
# SessionLocal itself (not just the get_db dependency) here means that
# independent session also lands on this same isolated in-memory test
# database instead of a second, untracked :memory: database.
database.SessionLocal = TestSessionLocal


@pytest.fixture
def client():
    """A FastAPI TestClient for making requests against the app,
    authenticated as a deterministic test user (alice). This is what
    keeps every pre-existing test passing unmodified: none of them send
    their own X-API-Key header, so they're all implicitly authenticated
    as alice now.
    """
    return TestClient(app, headers={"X-API-Key": TEST_API_KEY})


@pytest.fixture
def other_user_headers():
    """Headers for a second, distinct authenticated user (bob) - for
    cross-user isolation tests that need to act as someone other than
    the default `client` fixture's user.
    """
    return {"X-API-Key": OTHER_TEST_API_KEY}


@pytest.fixture
def unauthenticated_client():
    """A FastAPI TestClient that never sends X-API-Key, for testing the
    missing-header 401 case.
    """
    return TestClient(app)


@pytest.fixture
def test_user_id():
    """The user_id the `client` fixture is authenticated as - for tests
    that call app.services.task_service functions directly (which now
    require a user_id) rather than only through the HTTP client.
    """
    return TEST_USER_ID


@pytest.fixture
def other_test_user_id():
    return OTHER_TEST_USER_ID


@pytest.fixture
def test_api_key():
    return TEST_API_KEY


@pytest.fixture
def other_test_api_key():
    return OTHER_TEST_API_KEY


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


@pytest.fixture
def restart_client_factory(tmp_path):
    """Factory for simulating an application/container restart against
    durable on-disk SQLite state (requirement: state must survive a new
    TestClient/application lifecycle, a uvicorn restart, and a Docker
    restart with the same volume).

    Returns a context-manager function; each call opens a brand new
    engine, a brand new sessionmaker, and a brand new TestClient - fully
    independent Python objects with no shared engine/Session/connection
    from any previous call - bound to the same on-disk file path (unlike
    the `client` fixture's shared in-memory :memory: + StaticPool
    engine, a real file is what makes "independent process, same disk
    state" meaningful to prove). Entering the context also runs the
    app's real lifespan (init_db(), which now only *verifies* the
    schema - see app/services/schema_migration.py), exactly as a real
    process restart would.

    init_db() is deliberately never allowed to create or stamp a
    schema itself, so - as an explicitly-named test fixture, exactly
    the kind of caller README.md's "Database migrations (Alembic)"
    section carves out for Base.metadata.create_all() - this fixture
    bootstraps the schema itself (create_all + stamp_head_for_tests)
    before ever entering the TestClient context, so init_db()'s
    verify-only check finds a database it recognizes as current. Both
    calls are safe no-ops on the second (simulated-restart) invocation
    against the same on-disk file: create_all only ever adds missing
    tables, and stamp_head_for_tests only ever rewrites the same
    already-correct revision id.

    Overrides get_db, database.SessionLocal, and database.engine
    (mirroring evals/isolation.py's isolated_app_client), and restores
    the previous overrides on exit.
    """
    db_path = tmp_path / "restart_test.db"

    @contextmanager
    def _client():
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        def _override_get_db():
            db = session_local()
            try:
                yield db
            finally:
                db.close()

        previous_override = app.dependency_overrides.get(get_db)
        previous_session_local = database.SessionLocal
        previous_engine = database.engine

        app.dependency_overrides[get_db] = _override_get_db
        database.SessionLocal = session_local
        database.engine = engine
        Base.metadata.create_all(bind=engine)
        schema_migration.stamp_head_for_tests(engine)

        try:
            with TestClient(app, headers={"X-API-Key": TEST_API_KEY}) as test_client:
                yield test_client
        finally:
            if previous_override is not None:
                app.dependency_overrides[get_db] = previous_override
            else:
                app.dependency_overrides.pop(get_db, None)
            database.SessionLocal = previous_session_local
            database.engine = previous_engine
            engine.dispose()

    return _client


@pytest.fixture(autouse=True)
def reset_tasks_db():
    """Reset every table (tasks, agent_runs/steps, conversation_states)
    before and after every test.

    Dropping and recreating the tables gives each test an empty
    database and resets the autoincrement id sequence, replacing the
    old tasks_db.clear() / _next_id reset used by the in-memory store.
    This also resets conversation_memory's ConversationState rows, since
    that table is registered on the same Base - there is no separate
    reset needed for pending clarifications/confirmations/last_task_id.
    """
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Clear the in-memory rate limiter's per-user counters before and
    after every test - same reasoning as reset_tasks_db above, so a
    test in tests/test_rate_limit.py that exhausts alice's limit can
    never leak into an unrelated, later test that reuses the same
    user_id.
    """
    from app.services import rate_limiter

    rate_limiter.reset()
    yield
    rate_limiter.reset()

