"""X-API-Key authentication.

A request authenticates by presenting a key configured in
app.config.API_KEYS, which maps each key to a stable user_id. There is
no accounts/registration system - keys and their owning user_id are
entirely configured out-of-band (environment variables). See
app/config.py for the configuration format and validation rules.

Reads app.config.API_KEYS live (via `from app import config` +
config.API_KEYS, never a captured copy) so tests and the evaluation
framework can inject/restore additional keys at runtime without
reimporting this module - the same pattern agent_planner.py already
uses for agent_decision.DECISION_PROVIDER.

Raw key values are never logged, persisted, or echoed back anywhere in
this module - only the resolved (non-secret) user_id ever leaves
get_current_user, and HTTPException detail strings are fixed, generic
text.
"""

import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app import config

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass(frozen=True)
class AuthenticatedUser:
    """The identity resolved from a validated X-API-Key header."""

    user_id: str


def _lookup_user_id(candidate: str) -> str | None:
    """Constant-time-per-entry lookup: every configured key is compared
    with secrets.compare_digest instead of Python's default `==`/dict
    lookup, so a single comparison can't leak how many leading bytes of
    a candidate key matched a specific configured key. Iterating over
    every configured key (rather than short-circuiting via `in`/`==`) is
    what makes that guarantee hold for whichever key the candidate is
    ultimately compared against.
    """
    matched_user_id: str | None = None
    for configured_key, user_id in config.API_KEYS.items():
        if secrets.compare_digest(candidate, configured_key):
            matched_user_id = user_id
    return matched_user_id


def get_current_user(api_key: str | None = Security(_api_key_header)) -> AuthenticatedUser:
    if api_key is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-API-Key header")

    user_id = _lookup_user_id(api_key)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    return AuthenticatedUser(user_id=user_id)
