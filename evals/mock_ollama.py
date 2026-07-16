"""A deterministic stand-in for the Ollama decision/planning providers.

Patches the exact two I/O seams every existing Ollama pytest test already
mocks (ollama_decision_provider._call_ollama and
ollama_planner_provider._call_ollama_plan) with a stub that builds a
spec-shaped fake response *from the current case's own expected block* -
i.e. "assume the model would have said exactly the right thing", so the
mode measures whether the surrounding pipeline (parsing, validation,
safety gating, tracing) correctly handles a well-behaved model's answer.

This never bypasses safety: agent_planner.decide_plan's destructive-
intent guard runs before ollama_planner_provider.plan is ever called, so
a destructive_multi_step_rejection case never reaches this stub at all -
the real safety code decides that outcome, not the mock.
"""

import json

from app.services import agent_decision, agent_planner, ollama_decision_provider, ollama_planner_provider
from evals.cases import ExpectedOutcome


class MockOllamaProvider:
    """Use as a context manager for the whole evaluation run:

        with MockOllamaProvider() as mock:
            for case in cases:
                mock.set_expected(case.expected)
                ...

    Also forces agent_decision.DECISION_PROVIDER = "ollama" and
    agent_planner.MULTI_STEP_PLANNING_ENABLED = True for the block's
    duration, restoring the previous values on exit - the same values
    pytest monkeypatches for the equivalent unit tests, just done by hand
    since there's no monkeypatch fixture outside pytest.
    """

    def __init__(self) -> None:
        self._expected: ExpectedOutcome | None = None
        self._previous_decision_provider: str | None = None
        self._previous_multi_step_enabled: bool | None = None
        self._previous_call_ollama = None
        self._previous_call_ollama_plan = None

    def set_expected(self, expected: ExpectedOutcome) -> None:
        self._expected = expected

    def _fake_call_ollama(self, payload: dict) -> dict:
        expected = self._expected
        if expected is None or expected.selected_tool is None:
            return {"message": {"role": "assistant", "content": "", "tool_calls": []}, "done": True}
        return {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": expected.selected_tool, "arguments": expected.arguments or {}}}
                ],
            },
            "done": True,
        }

    def _fake_call_ollama_plan(self, payload: dict) -> dict:
        expected = self._expected
        steps = [{"tool": step.tool, "arguments": step.arguments} for step in (expected.step_tools or [])] if expected else []
        return {"message": {"role": "assistant", "content": json.dumps({"steps": steps})}, "done": True}

    def __enter__(self) -> "MockOllamaProvider":
        self._previous_decision_provider = agent_decision.DECISION_PROVIDER
        self._previous_multi_step_enabled = agent_planner.MULTI_STEP_PLANNING_ENABLED
        self._previous_call_ollama = ollama_decision_provider._call_ollama
        self._previous_call_ollama_plan = ollama_planner_provider._call_ollama_plan

        agent_decision.DECISION_PROVIDER = "ollama"
        agent_planner.MULTI_STEP_PLANNING_ENABLED = True
        ollama_decision_provider._call_ollama = self._fake_call_ollama
        ollama_planner_provider._call_ollama_plan = self._fake_call_ollama_plan
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        agent_decision.DECISION_PROVIDER = self._previous_decision_provider
        agent_planner.MULTI_STEP_PLANNING_ENABLED = self._previous_multi_step_enabled
        ollama_decision_provider._call_ollama = self._previous_call_ollama
        ollama_planner_provider._call_ollama_plan = self._previous_call_ollama_plan
