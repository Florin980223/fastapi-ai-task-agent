"""Offline evaluation suite for the AI task agent.

Separate from the pytest unit test suite: this package measures agent
*quality* against a versioned dataset of user messages and expected
outcomes, by driving the real FastAPI app (never reimplementing its
routing/decision/planning logic) in an isolated, temporary database.

Run with: python -m evals.run
"""

import os

# A deterministic eval API key/user, injected automatically so the
# evaluation framework never depends on whatever (if anything) a
# developer has configured in their own .env - matching the isolation
# principle evals/isolation.py already applies to the database.
# app.config.API_KEYS is parsed once, eagerly, at import time and
# requires at least one configured user, so this must run before any
# submodule of this package (evals.runner, evals.isolation, ...) can
# trigger that import - which is guaranteed here, since Python always
# executes a package's __init__.py before any of its submodules. Only
# set as a fallback (os.environ.setdefault, never overwriting a real
# value already exported, e.g. by tests/conftest.py or a developer's
# shell) - evals/isolation.py's isolated_app_client() still injects
# this same key at runtime regardless, so it's guaranteed present
# either way.
EVAL_API_KEY = "eval-key-do-not-use-in-prod"
EVAL_USER_ID = "eval-user"
os.environ.setdefault("API_KEYS", f"{EVAL_API_KEY}:{EVAL_USER_ID}")
