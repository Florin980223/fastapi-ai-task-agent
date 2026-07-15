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
