"""Optional, idempotent fictional-data seeder for the isolated local demo
(compose.demo.yaml) - see docs/LOCAL_DEMO.md.

Deliberately self-contained: never imports anything from app/ (which
would transitively import app.config and require API_KEYS to be set
just to seed data, and would require sys.path surgery since this script
lives under docker/, not the repo root). Talks to the demo PostgreSQL
database directly via a throwaway SQLAlchemy engine built from an
explicit URL, using raw parameterized SQL against the known `tasks`
table shape - never the ORM, never app.database's process-global
engine, never SQLite, never tasks.db.

Hardened guards (all must pass before any connection is even attempted):
- the target URL must not equal this process's own DATABASE_URL
  environment variable (protects against an accidentally-inherited
  "real" configured database)
- the URL scheme must be postgresql (or postgresql+<driver>)
- the host must be exactly "localhost" or "127.0.0.1"
- the database name must be exactly "taskagent_demo"
- --user-id must be exactly "demo_user_a" or "demo_user_b"

Idempotent: only ever inserts whichever of a small fixed set of
fictional, non-sensitive task titles is missing for the given user -
never touches, updates, or deletes a pre-existing row. --dry-run never
opens a write transaction at all - it only ever runs a read-only SELECT
to compute and print what would be inserted.
"""

import argparse
import os
import sys
from urllib.parse import urlsplit

from sqlalchemy import bindparam, create_engine, text

_ALLOWED_USER_IDS = ("demo_user_a", "demo_user_b")
_REQUIRED_DB_NAME = "taskagent_demo"
_ALLOWED_HOSTS = ("localhost", "127.0.0.1")

# Small, fixed, clearly fictional and non-sensitive - no Faker/similar
# dependency needed for a handful of demo task titles.
_SEED_TITLES = [
    "Buy milk",
    "Plan demo walkthrough",
    "Write release notes",
    "Review pull request",
    "Water the office plants",
]


class DemoSeedGuardError(RuntimeError):
    """Raised when the target doesn't look like the isolated demo
    database/user this script is allowed to touch. Never includes a raw
    password or the full connection string in its message - only the
    safe, non-secret parsed components (scheme, host, database name,
    user_id).
    """


def _validate_target(database_url: str, user_id: str) -> None:
    real_url = os.environ.get("DATABASE_URL")
    if real_url and database_url == real_url:
        raise DemoSeedGuardError(
            "the given database URL is identical to this process's own DATABASE_URL "
            "environment variable - point --database-url/DEMO_DATABASE_URL explicitly "
            "at the isolated demo database instead."
        )

    parsed = urlsplit(database_url)
    if parsed.scheme != "postgresql" and not parsed.scheme.startswith("postgresql+"):
        raise DemoSeedGuardError(f"database URL scheme must be postgresql, got {parsed.scheme!r}.")
    if parsed.hostname not in _ALLOWED_HOSTS:
        raise DemoSeedGuardError(f"database host must be one of {_ALLOWED_HOSTS}, got {parsed.hostname!r}.")
    db_name = parsed.path.lstrip("/")
    if db_name != _REQUIRED_DB_NAME:
        raise DemoSeedGuardError(f"database name must be exactly {_REQUIRED_DB_NAME!r}, got {db_name!r}.")
    if user_id not in _ALLOWED_USER_IDS:
        raise DemoSeedGuardError(f"user_id must be one of {_ALLOWED_USER_IDS}, got {user_id!r}.")


def _missing_titles(connection, user_id: str) -> list[str]:
    """Read-only - never writes anything. Returns the subset of
    _SEED_TITLES this user does not already have.
    """
    stmt = text("SELECT title FROM tasks WHERE user_id = :user_id AND title IN :titles").bindparams(
        bindparam("titles", expanding=True)
    )
    existing = set(connection.execute(stmt, {"user_id": user_id, "titles": _SEED_TITLES}).scalars().all())
    return [title for title in _SEED_TITLES if title not in existing]


def _insert_missing(connection, user_id: str, titles: list[str]) -> None:
    for title in titles:
        connection.execute(
            text("INSERT INTO tasks (user_id, title, description, done) VALUES (:user_id, :title, NULL, :done)"),
            {"user_id": user_id, "title": title, "done": False},
        )


def run_seed(database_url: str, user_id: str, dry_run: bool) -> list[str]:
    """Validates the target, then either previews (dry_run=True, zero
    writes, read-only connection only) or performs (dry_run=False, one
    transaction) the idempotent insert of whichever seed titles are
    missing for user_id. Returns the list of titles that were (or, in
    dry-run mode, would be) inserted - an empty list means nothing to do.
    """
    _validate_target(database_url, user_id)
    engine = create_engine(database_url)
    try:
        if dry_run:
            # Read-only connection - _insert_missing is never called on
            # this path, so no write/transaction is ever opened.
            with engine.connect() as connection:
                return _missing_titles(connection, user_id)

        with engine.begin() as connection:
            missing = _missing_titles(connection, user_id)
            if missing:
                _insert_missing(connection, user_id, missing)
            return missing
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--database-url",
        help="Explicit demo PostgreSQL URL. No default - falls back to DEMO_DATABASE_URL if omitted.",
    )
    parser.add_argument("--user-id", required=True, choices=_ALLOWED_USER_IDS)
    parser.add_argument("--dry-run", action="store_true", help="Preview only - never writes.")
    args = parser.parse_args(argv)

    database_url = args.database_url or os.environ.get("DEMO_DATABASE_URL")
    if not database_url:
        print(
            "ERROR: --database-url or DEMO_DATABASE_URL is required - there is no default.",
            file=sys.stderr,
        )
        return 1

    try:
        result = run_seed(database_url, args.user_id, args.dry_run)
    except DemoSeedGuardError as exc:
        print(f"ERROR: refusing to seed - {exc}", file=sys.stderr)
        return 1

    mode = "DRY RUN - would insert" if args.dry_run else "Inserted"
    if result:
        print(f"{mode} {len(result)} task(s) for {args.user_id}: {result}")
    else:
        print(f"{args.user_id} already has all seed tasks - nothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
