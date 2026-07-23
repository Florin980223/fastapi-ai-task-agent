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
    # Argument names the rule-based provider detected a reference to but
    # could not confidently resolve to a value (currently only ever
    # "due_date" - see agent_decision._extract_due_date). Deliberately a
    # decision-level field, never a value inside `arguments`: this makes
    # it structurally impossible for an Anthropic/Ollama tool call to ever
    # populate it (those providers build ToolDecision directly from the
    # model's own JSON tool-call arguments and never touch this field), so
    # there is no string value a confused/adversarial model could emit
    # that would ever be mistaken for it. clarification.missing_arguments
    # reads this to trigger a real clarification question; nothing here is
    # ever written to `arguments`, so it can never reach agent_service,
    # task_service, or the database, and never needs "removing" - it's
    # simply absent once a later reply supplies a real value and a fresh
    # ToolDecision/merge produces a clean `arguments` dict without it.
    needs_clarification_for: tuple[str, ...] = ()
