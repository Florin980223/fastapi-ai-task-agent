"""In-memory storage for pending multi-turn clarifications and remembered
conversation context.

Two independent stores, both keyed by conversation_id, both in-process
only (reset on restart, same as tasks_db in app/models.py - no database
or Redis involved):

- _pending: when POST /agent/execute can't complete a tool decision
  because a required argument is missing, the decision is parked here
  so the next request in the same conversation can supply just the
  missing piece (e.g. "3") instead of restating the whole request.
- _last_task_id: the most recent task id a successful create_task /
  update_task / mark_task_done identified, so a later referential
  message ("Mark it as done") can resolve "it" without asking again.
  Completely separate from _pending - cancelling a pending
  clarification does not touch this, and vice versa.
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


_last_task_id: dict[UUID, int] = {}

# Tools whose successful result unambiguously identifies exactly one
# task - worth remembering as "the task the user was just talking about".
_TASK_IDENTIFYING_TOOLS = {"create_task", "update_task", "mark_task_done"}


def get_last_task_id(conversation_id: UUID) -> int | None:
    return _last_task_id.get(conversation_id)


def record_result(conversation_id: UUID, selected_tool: str | None, result: object) -> None:
    """Update remembered context based on a tool's execution result.

    Only create_task/update_task/mark_task_done set it, and only on a
    genuine success (a dict, no "error" key, an integer id). delete_task
    clears it, but only when the id it actually deleted matches the
    remembered one - deleting some other, explicitly-targeted task
    leaves an unrelated remembered task alone. list_tasks (and anything
    else) is never touched.
    """
    if not isinstance(result, dict) or "error" in result:
        return

    if selected_tool in _TASK_IDENTIFYING_TOOLS:
        task_id = result.get("id")
        if isinstance(task_id, int):
            _last_task_id[conversation_id] = task_id
    elif selected_tool == "delete_task":
        deleted_id = result.get("task_id")
        if isinstance(deleted_id, int) and _last_task_id.get(conversation_id) == deleted_id:
            _last_task_id.pop(conversation_id, None)
