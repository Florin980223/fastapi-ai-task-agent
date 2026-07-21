"""Pure, dependency-free helpers for keeping the Neon runtime (pooled)
and migration (direct) database URLs safely separated - see
docs/VERCEL.md.

Every function here is a pure function: none of them read os.environ
themselves (the only caller that reads MIGRATION_MODE/
MIGRATION_DATABASE_URL from the process environment is
alembic/env.py), and none of them ever include a raw URL or credential
in an exception message - only fixed, safe text.
"""

from urllib.parse import parse_qs, urlsplit

_LOCAL_HOSTS = ("localhost", "127.0.0.1")


class PooledUrlForMigrationError(RuntimeError):
    """Raised when a URL intended for migrations looks like a Neon
    pooled (PgBouncer) endpoint - migrations must use the direct
    connection string instead. Defense in depth on top of requiring a
    distinctly-named MIGRATION_DATABASE_URL in the first place, not a
    replacement for it.
    """


class MigrationUrlRequiredError(RuntimeError):
    """Raised when MIGRATION_MODE=production is set but no migration
    database URL was supplied - Alembic must never silently fall back
    to the runtime's pooled DATABASE_URL for a production migration.
    """


class InsecureRemoteDatabaseUrlError(RuntimeError):
    """Raised when a non-local PostgreSQL URL is missing
    sslmode=require. A localhost/127.0.0.1 URL may omit it, which is
    what keeps local verification against a local Postgres usable
    without a locally-configured SSL certificate.
    """


def is_pooled_neon_url(url: str) -> bool:
    """True if the URL's hostname looks like a Neon pooled (PgBouncer)
    endpoint - Neon's own documented naming convention includes
    "-pooler" in the hostname of its pooled connection strings.
    """
    hostname = urlsplit(url).hostname or ""
    return "-pooler" in hostname


def _is_local_host(hostname: str | None) -> bool:
    return hostname in _LOCAL_HOSTS


def has_sslmode_require(url: str) -> bool:
    query = parse_qs(urlsplit(url).query)
    return "require" in query.get("sslmode", [])


def require_ssl_for_remote_postgres(url: str) -> None:
    """Refuses a non-local PostgreSQL URL that is missing
    sslmode=require. Never includes the raw URL or credentials in its
    exception message.
    """
    hostname = urlsplit(url).hostname
    if _is_local_host(hostname):
        return
    if not has_sslmode_require(url):
        raise InsecureRemoteDatabaseUrlError(
            "a non-local PostgreSQL connection must include sslmode=require in its query "
            "string - refusing to connect insecurely."
        )


def resolve_migration_url(
    migration_mode: str | None,
    migration_database_url: str | None,
    fallback_database_url: str,
) -> str:
    """Resolves which database URL Alembic should use.

    Never reads os.environ itself - the caller (alembic/env.py) is the
    only place that reads MIGRATION_MODE/MIGRATION_DATABASE_URL from
    the process environment; this function only ever uses its
    arguments.

    - migration_mode != "production": returns fallback_database_url
      unchanged - the exact same behavior as before this feature
      existed, for every local/Docker/CI Alembic invocation. No risk of
      regression, since none of those ever set MIGRATION_MODE.
    - migration_mode == "production": requires migration_database_url
      to be set (raises MigrationUrlRequiredError otherwise, never
      silently falling back to fallback_database_url), then validates
      it is not a pooled URL (raises PooledUrlForMigrationError) and
      has sslmode=require if non-local (raises
      InsecureRemoteDatabaseUrlError), before returning it.
    """
    normalized_mode = (migration_mode or "").strip().lower()
    if normalized_mode != "production":
        return fallback_database_url

    if not migration_database_url:
        raise MigrationUrlRequiredError(
            "MIGRATION_MODE=production requires MIGRATION_DATABASE_URL to be set - "
            "refusing to fall back to the runtime's pooled DATABASE_URL for a production "
            "migration."
        )

    if is_pooled_neon_url(migration_database_url):
        raise PooledUrlForMigrationError(
            "the migration database URL's hostname looks like a pooled Neon endpoint "
            "(contains '-pooler') - migrations must use the direct connection string instead."
        )

    require_ssl_for_remote_postgres(migration_database_url)

    return migration_database_url
