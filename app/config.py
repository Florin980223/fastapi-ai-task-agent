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

# The Claude model to use when DECISION_PROVIDER is "anthropic".
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")

# Where the local Ollama server is running, and which model to use when
# DECISION_PROVIDER is "ollama".
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b")
