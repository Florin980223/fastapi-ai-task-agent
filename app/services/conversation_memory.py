"""In-memory storage for pending multi-turn clarifications.

When POST /agent/execute can't complete a tool decision because a
required argument is missing, the decision is parked here, keyed by
conversation_id, so the next request in the same conversation can
supply just the missing piece (e.g. "3") instead of restating the
whole request. In-process only - resets on restart, same as tasks_db
in app/models.py. No database or Redis involved.
"""

from dataclasses import dataclass
from uuid import UUID


@dataclass
class PendingClarification:
    """The minimum needed to resume an incomplete tool decision."""

    selected_tool: str
    arguments: dict[str, str | int | bool | None]
    reason: str
    missing: list[str]


_pending: dict[UUID, PendingClarification] = {}


def get(conversation_id: UUID) -> PendingClarification | None:
    return _pending.get(conversation_id)


def set(conversation_id: UUID, pending: PendingClarification) -> None:
    _pending[conversation_id] = pending


def clear(conversation_id: UUID) -> None:
    _pending.pop(conversation_id, None)
