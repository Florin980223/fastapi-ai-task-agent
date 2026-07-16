"""Offline evaluation suite for the AI task agent.

Separate from the pytest unit test suite: this package measures agent
*quality* against a versioned dataset of user messages and expected
outcomes, by driving the real FastAPI app (never reimplementing its
routing/decision/planning logic) in an isolated, temporary database.

Run with: python -m evals.run
"""
