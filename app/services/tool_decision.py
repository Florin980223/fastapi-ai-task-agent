"""The shared internal contract between deciding what to do and doing it.

Kept in its own tiny module (rather than inside agent_decision.py) so
that both the rule-based provider and the Anthropic provider can import
it without importing each other.
"""

from pydantic import BaseModel


class ToolDecision(BaseModel):
    """What a decision provider thinks should happen for a message."""

    selected_tool: str | None
    arguments: dict[str, str | int | bool | None]
    reason: str
