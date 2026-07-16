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
    """Create all tables that don't exist yet, then patch any pre-existing
    table that predates the user_id column. Safe to call on every startup.
    """
    from app import db_models  # noqa: F401 - registers Task on Base.metadata
    from app.services.db_migrate import backfill_legacy_user_id_columns

    Base.metadata.create_all(bind=engine)
    backfill_legacy_user_id_columns(engine)
