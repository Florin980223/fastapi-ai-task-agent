"""SQLAlchemy engine/session setup.

This is the only module that knows about the database URL and engine
configuration. Everything else (services, routes) gets a Session via
the get_db() dependency and never talks to the engine directly.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL

# check_same_thread=False is required for SQLite because FastAPI runs
# sync route handlers in worker threads different from the thread that
# created the engine. Each request gets its own Session (see get_db),
# so no Session is ever shared across threads concurrently.
_is_sqlite = DATABASE_URL.startswith("sqlite")
connect_args = {"check_same_thread": False} if _is_sqlite else {}

# pool_pre_ping issues a cheap "is this connection still alive" check
# before handing a pooled connection out, transparently reconnecting if
# not. Pointless for SQLite (a local file, not a network service that
# can drop a connection), but a well-established safeguard for any real
# network database (e.g. PostgreSQL) - without it, a connection that
# went stale while idle (a Postgres restart, an idle timeout) would
# surface as a confusing driver error instead of a clean reconnect, most
# dangerously inside the best-effort tracing/last_task_id side-channel
# sessions (agent_trace_service.record_execute_run,
# conversation_memory.record_result), which already catch exceptions
# broadly and would otherwise just silently swallow it.
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=not _is_sqlite)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency that yields a Session and closes it afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Verify the database's schema is exactly what Alembic's latest
    migration expects. Safe to call on every startup - it never
    creates, alters, migrates, or stamps anything.

    Alembic (`python -m alembic upgrade head`, run by a human) is the
    only thing that changes a real application database's schema -
    see app.services.schema_migration and README.md's "Database
    migrations (Alembic)" section for the one-time adoption steps an
    existing pre-Alembic database needs before it can start here.
    Raises SchemaNotAdoptedError/SchemaOutOfDateError (both
    RuntimeError) with an actionable message rather than silently
    fixing anything, so a missing/outdated schema fails loudly instead
    of drifting.
    """
    from app import db_models  # noqa: F401 - registers all models on Base.metadata
    from app.services.schema_migration import ensure_schema_is_current

    ensure_schema_is_current(engine)
