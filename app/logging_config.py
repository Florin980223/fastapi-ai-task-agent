"""Central logging configuration: one key=value formatter for the whole
app, plus the request-id contextvar every log line is tagged with.

configure_logging() is called once, from app.main at import time. It
attaches a single logging.Filter (_RequestIdFilter) to the app's one
StreamHandler, which unconditionally sets record.request_id from
request_id_var - this means every existing logger.warning(...)/
logger.info(...) call already in the codebase (conversation_memory.py,
agent_trace_service.py, agent_decision.py, db_migrate.py, ...) gets
request_id=... in its output for free, with zero changes to those call
sites. app/middleware.py is the only thing that ever sets
request_id_var itself (one value per HTTP request, reset in a finally
block once that request is done).

Because the filter is attached to the handler (not to individual
loggers) and mutates the LogRecord in place, this also works correctly
under pytest's caplog: caplog attaches its own handler to the same
logger hierarchy and receives the very same LogRecord object our
handler's filter already mutated - regardless of which handler happens
to run first, by the time a test inspects caplog.records the mutation
has already happened, since all handlers for a given record are invoked
synchronously within one Logger.callHandlers() pass before control
returns to application code.
"""

import contextvars
import logging

from app.config import LOG_LEVEL

# Default "-" (not None) so a log line emitted outside any request (app
# startup, a background/best-effort session like conversation_memory's
# own SessionLocal()) renders a clean "request_id=-" instead of "None".
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

# Fields that only the one dedicated access-log line (app/middleware.py)
# ever sets via logger.info(..., extra={...}) - appended to the base
# line only when present, so every other module's plain-text log calls
# are rendered unchanged.
_ACCESS_LOG_EXTRA_FIELDS = ("method", "path", "status", "duration_ms", "user_id")


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class _KeyValueFormatter(logging.Formatter):
    """Renders every record as one greppable key=value line - no JSON
    dependency, readable directly in `docker compose logs` / a local
    terminal. Never includes the raw msg format args beyond what
    record.getMessage() already safely interpolates (the same text any
    default formatter would show) - callers are responsible for never
    passing secrets into a log call in the first place (see each
    module's own docstring for that guarantee).
    """

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, "%Y-%m-%dT%H:%M:%S")
        line = (
            f"{timestamp}.{int(record.msecs):03d}Z "
            f"level={record.levelname} logger={record.name} "
            f"request_id={getattr(record, 'request_id', '-')} "
            f'msg="{record.getMessage()}"'
        )
        extras = " ".join(
            f"{field}={getattr(record, field)}" for field in _ACCESS_LOG_EXTRA_FIELDS if hasattr(record, field)
        )
        if extras:
            line = f"{line} {extras}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


_configured = False


def configure_logging() -> None:
    """Configure the app's logging exactly once per process.

    Idempotent by design: app.main calls this at import time, but the
    module could plausibly be imported more than once in a long-lived
    process (or a test/eval runner re-importing app.main) - a second
    call must never install a second handler (which would duplicate
    every single log line).
    """
    global _configured
    if _configured:
        return

    handler = logging.StreamHandler()
    handler.setFormatter(_KeyValueFormatter())
    handler.addFilter(_RequestIdFilter())

    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    root.addHandler(handler)

    _configured = True
