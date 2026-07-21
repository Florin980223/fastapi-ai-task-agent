"""Tests for app.main's FastAPI lifespan: graceful startup/shutdown,
preserved fail-closed startup schema verification, and bounded shutdown
even when a readiness check is permanently stuck.
"""

import threading
import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from sqlalchemy.orm import sessionmaker

import app.config as config
import app.database as database
from app.database import Base, get_db
from app.main import app
from app.services import readiness, schema_migration


@contextmanager
def _app_client_against(engine):
    """Minimal, self-contained variant of tests/conftest.py's
    restart_client_factory: swaps app.database.engine/SessionLocal and
    the get_db dependency override for the duration of the context, and
    enters TestClient(app) as a real context manager so the app's actual
    lifespan (startup + shutdown) runs. Deliberately never calls
    engine.dispose() itself anywhere in this helper (unlike
    restart_client_factory, which does as its own extra safety net), so
    tests using it can assert exactly how many times app.main's own
    lifespan shutdown disposes the engine, without a second,
    fixture-level dispose() call inflating the count.
    """
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

    try:
        with TestClient(app) as client:
            yield client
    finally:
        if previous_override is not None:
            app.dependency_overrides[get_db] = previous_override
        else:
            app.dependency_overrides.pop(get_db, None)
        database.SessionLocal = previous_session_local
        database.engine = previous_engine


@pytest.fixture(autouse=True)
def _reset_readiness_slot():
    readiness.shutdown()
    yield
    readiness.shutdown()


def test_graceful_startup_and_shutdown_succeed(tmp_path):
    db_path = tmp_path / "lifespan_ok.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    schema_migration.stamp_head(engine)
    try:
        with _app_client_against(engine) as client:
            response = client.get("/health")
            assert response.status_code == 200
    finally:
        engine.dispose()


def test_shutdown_disposes_engine_and_readiness_exactly_once(tmp_path, monkeypatch):
    db_path = tmp_path / "lifespan_dispose.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    schema_migration.stamp_head(engine)

    dispose_calls = []
    original_dispose = engine.dispose

    def spy_dispose(*args, **kwargs):
        dispose_calls.append(True)
        return original_dispose(*args, **kwargs)

    monkeypatch.setattr(engine, "dispose", spy_dispose)

    shutdown_calls = []
    original_shutdown = readiness.shutdown

    def spy_shutdown():
        shutdown_calls.append(True)
        return original_shutdown()

    monkeypatch.setattr(readiness, "shutdown", spy_shutdown)

    with _app_client_against(engine):
        pass  # lifespan startup runs on enter, shutdown runs on exit

    assert dispose_calls == [True]
    assert shutdown_calls == [True]


def test_startup_fails_when_schema_is_stale(tmp_path):
    """Fail-closed startup verification must be preserved exactly:
    lifespan's init_db() -> ensure_schema_is_current() must still raise
    uncaught when the schema isn't at Alembic head (here: never stamped
    at all), and the application must never start serving in that case -
    there is no partially-running process where /health would answer
    200. This is distinct from a *post-startup* database failure (see
    tests/test_readiness.py::test_health_stays_200_independent_of_a_not_ready_database),
    which is the only case /health is meant to stay independent of.
    """
    db_path = tmp_path / "lifespan_stale.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    # Deliberately never stamped - an unadopted (or, equally, a stale)
    # schema must still block startup entirely.

    with pytest.raises(schema_migration.SchemaNotAdoptedError):
        with _app_client_against(engine):
            pytest.fail("startup should have raised before this line ever runs")

    engine.dispose()


def test_shutdown_stays_bounded_when_readiness_check_is_permanently_stuck(tmp_path, monkeypatch):
    """A hung database readiness check must never block graceful
    application shutdown - see app/services/readiness.py's daemon-thread
    design (not concurrent.futures.ThreadPoolExecutor, whose atexit hook
    would otherwise join a stuck worker and hang process/interpreter
    exit, i.e. graceful SIGTERM shutdown).
    """
    db_path = tmp_path / "lifespan_stuck_ready.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    schema_migration.stamp_head(engine)

    never = threading.Event()  # deliberately never set

    def _forever(_engine):
        never.wait()

    monkeypatch.setattr(readiness, "ensure_schema_is_current", _forever)
    monkeypatch.setattr(config, "READY_CHECK_TIMEOUT_SECONDS", 0.05)

    try:
        with _app_client_against(engine) as client:
            response = client.get("/ready")
            assert response.status_code == 503
            assert response.json()["reason"] == "timeout"

            started = time.monotonic()
        # __exit__ of _app_client_against (below this line) runs the
        # real lifespan shutdown, while the check above is still stuck.
        elapsed = time.monotonic() - started
    finally:
        engine.dispose()

    assert elapsed < 1.0
