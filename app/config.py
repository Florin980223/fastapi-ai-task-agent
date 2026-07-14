"""Central place for reading configuration from the environment.

Nothing else in the codebase should call os.environ.get(...) for these
settings - read them from here instead, so there's exactly one place
to look when changing how the app is configured.
"""

import os

from dotenv import load_dotenv

load_dotenv()  # no-op if there's no .env file; loads one if present

# Which decision provider POST /agent/decide-tool and POST /agent/execute
# use: "rule_based" (default, safe, no external calls) or "anthropic"
# (asks Claude to pick a tool - see app/services/anthropic_decision_provider.py).
DECISION_PROVIDER = os.environ.get("AGENT_DECISION_PROVIDER", "rule_based").strip().lower()

# The Claude model to use when DECISION_PROVIDER is "anthropic".
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
