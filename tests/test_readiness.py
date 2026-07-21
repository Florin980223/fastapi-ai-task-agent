"""Tests for app.services.readiness.check_ready() at the service layer -
no HTTP client needed for these. HTTP-level GET /ready endpoint tests
(status codes, security headers, response-body redaction, liveness
independence) are added further down in this same file.
"""

import logging
import threading
import time

import pytest
from sqlalchemy import create_engine

from app import config, database
from app.database import Base
from app.services import readiness, schema_migration


@pytest.fixture
def adopted_engine(tmp_path):
    """A real on-disk SQLite engine, schema created and stamped at
    Alembic head - exactly what ensure_schema_is_current() expects to
    see as "ready". A real file (not sqlite:///:memory:) is required
    because schema_migration.stamp_head() opens its own connection from
    the engine's URL string rather than reusing the passed-in engine
    object - a fresh in-memory database would come up empty instead.
    Swaps app.database.engine for the duration of the test (mirroring
    tests/conftest.py's restart_client_factory) and restores/disposes it
    afterwards.
    """
    db_path = tmp_path / "readiness_adopted.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    schema_migration.stamp_head(engine)

    previous_engine = database.engine
    database.engine = engine
    try:
        yield engine
    finally:
        database.engine = previous_engine
        engine.dispose()


@pytest.fixture(autouse=True)
def _reset_readiness_slot():
    """The single-slot in-flight tracking in app.services.readiness is
    module-level state, not per-test - clear it before and after every
    test so one test's check can never be seen as "still in progress" by
    the next.
    """
    readiness.shutdown()
    yield
    readiness.shutdown()


def test_check_ready_success(adopted_engine):
    result = readiness.check_ready()
    assert result.ready is True
    assert result.reason is None


def test_check_ready_schema_not_adopted(tmp_path, monkeypatch):
    # Tables exist but were never stamped by Alembic - the "has data but
    # was never adopted" branch of ensure_schema_is_current().
    db_path = tmp_path / "readiness_unadopted.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    try:
        result = readiness.check_ready()
    finally:
        engine.dispose()
    assert result.ready is False
    assert result.reason == "schema_not_adopted"


def test_check_ready_schema_out_of_date(monkeypatch):
    def _raise(_engine):
        raise schema_migration.SchemaOutOfDateError("revision mismatch detail - never surfaced")

    monkeypatch.setattr(readiness, "ensure_schema_is_current", _raise)
    result = readiness.check_ready()
    assert result.ready is False
    assert result.reason == "schema_out_of_date"


def test_check_ready_database_unavailable_redacts_exception_text(monkeypatch):
    secret = "connection to postgresql://real-user:hunter2@db.internal/prod failed"

    def _raise(_engine):
        raise RuntimeError(secret)

    monkeypatch.setattr(readiness, "ensure_schema_is_current", _raise)
    result = readiness.check_ready()
    assert result.ready is False
    assert result.reason == "database_unavailable"
    assert "hunter2" not in (result.reason or "")
    assert "postgresql://" not in (result.reason or "")


def test_check_ready_never_logs_raw_exception_details(monkeypatch, caplog):
    """app/services/readiness.py must never call logger.exception() or
    set exc_info=True (app/logging_config.py's _KeyValueFormatter appends
    the full traceback to the log line whenever exc_info is set), and
    must never interpolate the caught exception into any log call - a
    database driver's own error text can embed the DATABASE_URL,
    hostname, username, or database name. Uses a sentinel that looks
    exactly like a real leak to make sure nothing smuggles it through
    the message, positional args, or any structured `extra` field.
    """
    sentinel = "SENTINEL-9f3a2b1c-postgresql://realuser:realpass@real-host:5432/realdb"

    def _raise(_engine):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(readiness, "ensure_schema_is_current", _raise)

    with caplog.at_level(logging.WARNING):
        result = readiness.check_ready()

    assert result.ready is False
    assert result.reason == "database_unavailable"

    assert len(caplog.records) >= 1
    for record in caplog.records:
        assert sentinel not in record.getMessage()
        assert sentinel not in str(record.args)
        assert record.exc_info is None
        for value in record.__dict__.values():
            assert sentinel not in str(value)


def test_check_ready_config_unavailable(monkeypatch):
    monkeypatch.setattr(config, "API_KEYS", {})
    result = readiness.check_ready()
    assert result.ready is False
    assert result.reason == "config_unavailable"


def test_check_ready_timeout(monkeypatch):
    monkeypatch.setattr(config, "READY_CHECK_TIMEOUT_SECONDS", 0.05)

    def _slow(_engine):
        time.sleep(0.5)

    monkeypatch.setattr(readiness, "ensure_schema_is_current", _slow)
    started = time.monotonic()
    result = readiness.check_ready()
    elapsed = time.monotonic() - started
    assert result.ready is False
    assert result.reason == "timeout"
    assert elapsed < 0.3


def test_check_ready_concurrent_probes_never_exceed_one_worker(monkeypatch):
    """Proves the fix for the original ThreadPoolExecutor-per-request
    design: repeated/concurrent readiness probes during a slow check must
    never start more than one background check and must never queue -
    every caller after the first gets check_in_progress immediately.
    """
    monkeypatch.setattr(config, "READY_CHECK_TIMEOUT_SECONDS", 5.0)
    release = threading.Event()
    call_count = {"n": 0}
    call_lock = threading.Lock()

    def _blocking(_engine):
        with call_lock:
            call_count["n"] += 1
        release.wait(timeout=5.0)

    monkeypatch.setattr(readiness, "ensure_schema_is_current", _blocking)

    results: list = []
    results_lock = threading.Lock()

    def _probe():
        result = readiness.check_ready()
        with results_lock:
            results.append(result)

    first = threading.Thread(target=_probe)
    first.start()
    time.sleep(0.1)  # let the first probe actually enter the blocking check

    followers = [threading.Thread(target=_probe) for _ in range(5)]
    for t in followers:
        t.start()
    for t in followers:
        t.join(timeout=2.0)

    # All follower probes must have returned immediately with
    # check_in_progress - none of them queue behind the first, and none
    # of them trigger a second background check.
    assert len(results) == 5
    assert all(r.reason == "check_in_progress" and r.ready is False for r in results)
    with call_lock:
        assert call_count["n"] == 1

    release.set()
    first.join(timeout=5.0)
    assert len(results) == 6


def test_shutdown_does_not_block_on_permanently_stuck_check(monkeypatch):
    """readiness.shutdown() must return within a small bounded time even
    while a check is permanently stuck - it must never join the
    background thread (daemon threads are abandoned at interpreter exit,
    never joined), which is what keeps a hung database connection from
    blocking graceful application shutdown.
    """
    never = threading.Event()  # deliberately never set

    def _forever(_engine):
        never.wait()

    monkeypatch.setattr(readiness, "ensure_schema_is_current", _forever)
    monkeypatch.setattr(config, "READY_CHECK_TIMEOUT_SECONDS", 0.05)

    result = readiness.check_ready()
    assert result.reason == "timeout"  # caller unblocks even though the check itself is still stuck

    started = time.monotonic()
    readiness.shutdown()
    elapsed = time.monotonic() - started
    assert elapsed < 0.5


# --- HTTP-level GET /ready tests -------------------------------------
#
# The default test suite's app.database.engine (distinct from the
# StaticPool test_engine the get_db dependency is overridden to use -
# see tests/conftest.py) is a plain, untouched, empty sqlite:///:memory:
# engine created once at app.database import time: no tables, so
# ensure_schema_is_current() correctly reports it as not-yet-adopted.
# GET /ready calls app.services.readiness.check_ready() directly (not
# through the get_db override), so it naturally sees this "not ready"
# state without any extra setup - exactly the scenario several tests
# below rely on. The adopted_engine fixture above is used instead
# whenever a test needs the "ready" (200) case.


def test_ready_endpoint_returns_200_when_adopted_and_current(unauthenticated_client, adopted_engine):
    response = unauthenticated_client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_endpoint_returns_503_when_schema_not_adopted(unauthenticated_client):
    response = unauthenticated_client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "reason": "schema_not_adopted"}


def test_ready_endpoint_does_not_require_an_api_key(unauthenticated_client):
    # Whatever the readiness outcome (200 or 503), it must never be
    # gated behind auth - an orchestrator/load balancer probe typically
    # can't supply an API key, matching /health's existing precedent.
    response = unauthenticated_client.get("/ready")
    assert response.status_code != 401


def test_ready_endpoint_never_leaks_exception_text_in_response(unauthenticated_client, monkeypatch):
    secret = "connection to postgresql://real-user:hunter2@db.internal/prod failed"

    def _raise(_engine):
        raise RuntimeError(secret)

    monkeypatch.setattr(readiness, "ensure_schema_is_current", _raise)
    response = unauthenticated_client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "reason": "database_unavailable"}
    assert secret not in response.text
    assert "hunter2" not in response.text
    assert "Traceback" not in response.text


def test_health_stays_200_independent_of_a_not_ready_database(unauthenticated_client):
    """Post-startup liveness/readiness split: /health never touches the
    database, so it stays 200 even while /ready correctly reports
    not-ready (here: the default suite's unadopted app.database.engine).
    This is distinct from a startup-time schema failure, which prevents
    the app from starting at all - see tests/test_lifespan.py for that
    case, and docs/DEPLOYMENT.md's "Health vs. readiness" section.
    """
    health_response = unauthenticated_client.get("/health")
    ready_response = unauthenticated_client.get("/ready")
    assert health_response.status_code == 200
    assert ready_response.status_code == 503


def test_ready_endpoint_has_security_headers_when_ready(unauthenticated_client, adopted_engine):
    response = unauthenticated_client.get("/ready")
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in response.headers


def test_ready_endpoint_has_security_headers_when_not_ready(unauthenticated_client):
    response = unauthenticated_client.get("/ready")
    assert response.status_code == 503
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in response.headers


def test_ready_endpoint_does_not_modify_the_database(unauthenticated_client, adopted_engine):
    from sqlalchemy import text

    with adopted_engine.connect() as connection:
        before = connection.execute(text("SELECT COUNT(*) FROM tasks")).scalar_one()

    for _ in range(3):
        response = unauthenticated_client.get("/ready")
        assert response.status_code == 200

    with adopted_engine.connect() as connection:
        after = connection.execute(text("SELECT COUNT(*) FROM tasks")).scalar_one()

    assert before == after == 0
