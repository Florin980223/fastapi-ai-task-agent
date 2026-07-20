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
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
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
