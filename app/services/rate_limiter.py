"""In-memory, per-user, fixed-window rate limiter for POST /agent/execute.

Deliberately NOT a security boundary and NOT correct across multiple
uvicorn workers - each worker would keep its own independent counter,
so real throughput becomes ~N times the configured limit with N
workers, and a single user's requests could land on different workers
unpredictably. This project runs exactly one worker (see Dockerfile),
so that limitation doesn't apply today; it exists to catch accidental
abuse (e.g. a buggy client stuck in a retry loop) in the configuration
this project actually runs, not to defend against a determined
attacker - who could simply wait out the window, and who is already
excluded by X-API-Key auth in the first place.

Keyed by user_id (the resolved, non-secret identity from
AuthenticatedUser) only - the raw API key is never available here and
never logged/stored anywhere in this module, matching the same
guarantee app/services/auth.py and app/services/conversation_memory.py
already give.

Reads app.config.RATE_LIMIT_* live (via `from app import config` +
config.RATE_LIMIT_ENABLED, never a captured copy) - the same pattern
app.services.auth already uses for config.API_KEYS - so tests can
monkeypatch app.config directly without reimporting this module.
"""

import math
import threading
import time as _time

from fastapi import Depends, HTTPException

from app import config
from app.services.auth import AuthenticatedUser, get_current_user

# Bound to the real time.monotonic at import time (not just `import
# time` + time.monotonic() calls) so tests can monkeypatch this
# module's own _monotonic in isolation, without also perturbing
# app/middleware.py's request-duration timing or any other module that
# happens to call time.monotonic() during the same test.
_monotonic = _time.monotonic

_lock = threading.Lock()
_counters: dict[str, tuple[int, float]] = {}  # user_id -> (count_in_window, window_start)


def reset() -> None:
    """Clear all rate-limit state. Test-only: gives every test a clean
    slate regardless of what an earlier test did for the same user_id -
    same reasoning tests/conftest.py's reset_tasks_db fixture gives for
    the task/conversation tables.
    """
    with _lock:
        _counters.clear()


def _check(user_id: str) -> None:
    if not config.RATE_LIMIT_ENABLED:
        return

    now = _monotonic()
    with _lock:
        count, window_start = _counters.get(user_id, (0, now))
        if now - window_start >= config.RATE_LIMIT_WINDOW_SECONDS:
            count, window_start = 0, now

        count += 1
        _counters[user_id] = (count, window_start)

        if count > config.RATE_LIMIT_REQUESTS:
            remaining = config.RATE_LIMIT_WINDOW_SECONDS - (now - window_start)
            retry_after = max(1, math.ceil(remaining))
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please slow down and try again shortly.",
                headers={"Retry-After": str(retry_after)},
            )


def enforce_execute_rate_limit(current_user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    """FastAPI dependency: enforces the fixed-window limit for the
    authenticated user, then returns them unchanged - a drop-in
    replacement for Depends(get_current_user) on POST /agent/execute.
    """
    _check(current_user.user_id)
    return current_user
