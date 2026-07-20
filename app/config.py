"""Central place for reading configuration from the environment.

Nothing else in the codebase should call os.environ.get(...) for these
settings - read them from here instead, so there's exactly one place
to look when changing how the app is configured.
"""

import os

from dotenv import load_dotenv

load_dotenv()  # no-op if there's no .env file; loads one if present

# Which decision provider POST /agent/decide-tool and POST /agent/execute
# use: "rule_based" (default, safe, no external calls), "anthropic" (asks
# Claude to pick a tool - app/services/anthropic_decision_provider.py), or
# "ollama" (asks a local Ollama model - app/services/ollama_decision_provider.py).
DECISION_PROVIDER = os.environ.get("AGENT_DECISION_PROVIDER", "rule_based").strip().lower()

# Whether POST /agent/execute may return a multi-step plan (up to 3
# sequential existing-tool calls) instead of a single tool call. Only
# takes effect when DECISION_PROVIDER is "ollama" - rule_based and
# anthropic remain single-step only in this first implementation.
MULTI_STEP_PLANNING_ENABLED = os.environ.get("AGENT_MULTI_STEP_PLANNING", "false").strip().lower() == "true"

# The Claude model to use when DECISION_PROVIDER is "anthropic".
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")

# Where the local Ollama server is running, and which model to use when
# DECISION_PROVIDER is "ollama".
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b")

# Where task data is persisted. Defaults to a local SQLite file so the
# app works out of the box with no extra setup.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./tasks.db")

# Reserved user_id assigned to rows migrated from a pre-authentication
# database (see app/services/db_migrate.py). Can never be assigned to a
# real configured user - see _parse_api_keys below - so legacy rows
# become inert/inaccessible via the API instead of silently landing on
# whichever real user happens to authenticate first.
UNMIGRATED_USER_ID = "__unmigrated__"


class ApiKeyConfigError(RuntimeError):
    """Raised when API_KEYS is missing or malformed. Always fails fast at
    import time - an app that can't authenticate anyone should never
    start serving requests. Never includes a raw API key value: only
    entry positions and (non-secret) user ids are ever mentioned.
    """


def _parse_api_keys(raw: str) -> dict[str, str]:
    """Parse "key1:user_id1,key2:user_id2,..." into {key: user_id}.

    Strict by design (see app/services/auth.py for how this is used):
    - the whole value must be non-empty (at least one user must be
      configured, or nothing can ever authenticate)
    - every entry must be "key:user_id" with a non-empty key and a
      non-empty user_id
    - user_id may not equal UNMIGRATED_USER_ID (reserved)
    - keys must be unique

    Raises ApiKeyConfigError on any violation. Never puts a raw key
    value into an error message - only the entry's 1-based position and
    (non-secret) user id are ever mentioned.
    """
    raw = raw.strip()
    if not raw:
        raise ApiKeyConfigError(
            'API_KEYS is not configured. Set it to a comma-separated list of "key:user_id" '
            "pairs (see .env.example) - at least one user must be configured."
        )

    api_keys: dict[str, str] = {}
    for index, entry in enumerate(raw.split(","), start=1):
        entry = entry.strip()
        if not entry:
            raise ApiKeyConfigError(f"API_KEYS entry #{index} is empty.")
        if ":" not in entry:
            raise ApiKeyConfigError(f'API_KEYS entry #{index} is malformed (expected "key:user_id").')

        key, _, user_id = entry.partition(":")
        key = key.strip()
        user_id = user_id.strip()

        if not key:
            raise ApiKeyConfigError(f"API_KEYS entry #{index} has an empty key.")
        if not user_id:
            raise ApiKeyConfigError(f"API_KEYS entry #{index} has an empty user_id.")
        if ":" in user_id:
            raise ApiKeyConfigError(f"API_KEYS entry #{index} has more than one ':' separator.")
        if user_id == UNMIGRATED_USER_ID:
            raise ApiKeyConfigError(
                f"API_KEYS entry #{index} uses the reserved user_id '{UNMIGRATED_USER_ID}', "
                "which cannot be assigned to a real user."
            )
        if key in api_keys:
            raise ApiKeyConfigError(f"API_KEYS entry #{index} duplicates an already-configured key.")

        api_keys[key] = user_id

    return api_keys


# Maps API keys to user ids for the X-API-Key auth dependency
# (app/services/auth.py). Every request to a protected endpoint must
# present a key that appears here. Raw key values are never logged or
# persisted anywhere - see app/services/auth.py.
API_KEYS: dict[str, str] = _parse_api_keys(os.environ.get("API_KEYS", ""))


class ConversationStateConfigError(RuntimeError):
    """Raised when a conversation-state TTL env var is set but isn't a
    positive integer. Always fails fast at import time, same reasoning
    as ApiKeyConfigError - a bad TTL should never silently fall back to
    some other value and start serving requests.
    """


def _parse_positive_int_seconds(name: str, raw: str) -> int:
    raw = raw.strip()
    try:
        value = int(raw)
    except ValueError:
        raise ConversationStateConfigError(f"{name} must be a positive integer number of seconds, got {raw!r}.")
    if value <= 0:
        raise ConversationStateConfigError(f"{name} must be a positive integer number of seconds, got {value}.")
    return value


# How long a pending destructive-action confirmation (e.g. delete_task)
# stays valid before a "yes" reply is treated as stale and ignored.
# Shortest of the three TTLs, since it gates an irreversible action.
CONFIRMATION_TTL_SECONDS = _parse_positive_int_seconds(
    "CONFIRMATION_TTL_SECONDS", os.environ.get("CONFIRMATION_TTL_SECONDS", "300")
)

# How long a pending clarification (a tool decision missing a required
# argument) stays valid before a follow-up reply is treated as an
# unrelated new message instead of an answer to it.
CLARIFICATION_TTL_SECONDS = _parse_positive_int_seconds(
    "CLARIFICATION_TTL_SECONDS", os.environ.get("CLARIFICATION_TTL_SECONDS", "900")
)

# How long a remembered last_task_id (for resolving "it"/"that one")
# stays valid. Longest of the three - purely a UX convenience, not
# safety-critical, so it's worth remembering across a longer gap.
CONTEXT_TTL_SECONDS = _parse_positive_int_seconds(
    "CONTEXT_TTL_SECONDS", os.environ.get("CONTEXT_TTL_SECONDS", "7200")
)
