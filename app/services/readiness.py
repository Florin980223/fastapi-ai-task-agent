"""Readiness check for GET /ready - verifies the app can safely receive
traffic (database reachable, schema at the expected Alembic revision,
required configuration present) without ever mutating the database.

Deliberately built on a single daemon threading.Thread slot rather than
concurrent.futures.ThreadPoolExecutor: that module registers a
process-wide atexit hook that joins every non-daemon worker thread it
ever created, regardless of how any individual executor.shutdown() call
is invoked - a readiness check stuck against a hung database connection
would silently block interpreter exit (and therefore graceful SIGTERM
shutdown) no matter what. A bare daemon thread is never joined by that
hook or by normal interpreter shutdown, so a permanently-stuck check can
never block process exit - see shutdown() below.

At most one check ever runs at a time - a request that arrives while one
is already in flight gets an immediate "check_in_progress" result rather
than starting a second thread or queuing behind the first, so repeated
probes during a sustained outage can never accumulate unbounded
background work.

Startup-time schema verification (app.database.init_db(), called from
app.main's lifespan before the app starts serving) is separate and
unaffected by this module: if the schema is invalid at startup, that
exception is never caught there, and the application never starts
serving at all. This module only ever runs *after* a successful startup,
to detect the database becoming unavailable later - see this project's
deployment runbook for the precise liveness-vs-readiness semantics.
"""

import logging
import threading
from dataclasses import dataclass

from app import config, database
from app.services.schema_migration import (
    SchemaNotAdoptedError,
    SchemaOutOfDateError,
    ensure_schema_is_current,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    reason: str | None = None


class _ConfigUnavailableError(RuntimeError):
    """Raised internally when required configuration looks unavailable.
    In practice near-impossible to hit: app.config already fails fast at
    import time, so if the process is running at all, config is already
    valid. Checked anyway as a cheap, harmless assertion.
    """


_state_lock = threading.Lock()
_active_thread: threading.Thread | None = None


def _check_config() -> None:
    if not config.API_KEYS or not config.DATABASE_URL:
        raise _ConfigUnavailableError("required configuration is unavailable")


def _run_check(event: threading.Event, box: dict) -> None:
    try:
        _check_config()
        # Looked up on the database module at call time (not imported by
        # name at module load time) so this stays correct in tests that
        # swap app.database.engine at runtime (see
        # tests/conftest.py's restart_client_factory), the same pattern
        # app.database.init_db() itself relies on.
        ensure_schema_is_current(database.engine)
        box["result"] = ReadinessResult(ready=True)
    except _ConfigUnavailableError:
        box["result"] = ReadinessResult(ready=False, reason="config_unavailable")
    except SchemaNotAdoptedError:
        box["result"] = ReadinessResult(ready=False, reason="schema_not_adopted")
    except SchemaOutOfDateError:
        box["result"] = ReadinessResult(ready=False, reason="schema_out_of_date")
    except Exception:
        # Never logs the exception itself (message, args, or traceback) -
        # a database driver's error text can embed the DATABASE_URL,
        # hostname, username, or database name (e.g. a psycopg
        # connection-failure message). logger.exception()/exc_info=True
        # is deliberately never used anywhere in this module:
        # app/logging_config.py's _KeyValueFormatter appends the full
        # formatted traceback to the log line whenever record.exc_info
        # is set, which would defeat this guarantee. Only ever this
        # fixed, safe event/reason pair - never included in the
        # ReadinessResult either, which is what the HTTP layer sends
        # back to the client.
        logger.warning(
            "readiness_check_failed",
            extra={"event": "readiness_check_failed", "reason": "database_unavailable"},
        )
        box["result"] = ReadinessResult(ready=False, reason="database_unavailable")
    finally:
        event.set()


def check_ready() -> ReadinessResult:
    """Runs (or reports as busy) the single readiness-check slot and
    returns a result within config.READY_CHECK_TIMEOUT_SECONDS. Never
    raises, never modifies the database, never includes a DB URL,
    credential, or raw exception text in its result - only a fixed,
    safe reason string.
    """
    global _active_thread
    with _state_lock:
        if _active_thread is not None and _active_thread.is_alive():
            return ReadinessResult(ready=False, reason="check_in_progress")
        event = threading.Event()
        box: dict = {}
        thread = threading.Thread(target=_run_check, args=(event, box), daemon=True)
        _active_thread = thread
        thread.start()

    finished = event.wait(timeout=config.READY_CHECK_TIMEOUT_SECONDS)
    if not finished:
        return ReadinessResult(ready=False, reason="timeout")
    return box["result"]


def shutdown() -> None:
    """Best-effort, always-fast cleanup for application shutdown. Never
    joins a possibly-hung check thread - daemon threads are abandoned at
    interpreter exit rather than joined, so this is safe to call
    unconditionally regardless of whether a check is currently stuck.
    """
    global _active_thread
    with _state_lock:
        _active_thread = None
