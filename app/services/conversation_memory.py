"""In-memory storage for pending multi-turn clarifications, pending
destructive-action confirmations, and remembered conversation context.

Three independent stores, all keyed by (user_id, conversation_id), all
in-process only (reset on restart, same as tasks_db in app/models.py -
no database or Redis involved):

- _pending: when POST /agent/execute can't complete a tool decision
  because a required argument is missing, the decision is parked here
  so the next request in the same conversation can supply just the
  missing piece (e.g. "3") instead of restating the whole request.
- _pending_confirmation: when a decision is complete but selects a
  destructive tool (e.g. delete_task), it is parked here instead of
  being executed, so the next request in the same conversation must
  explicitly confirm ("yes") or cancel ("no") it. Completely separate
  from _pending - a conversation is only ever waiting on one of the
  two at a time, but clearing/resolving one never touches the other.
- _last_task_id: the most recent task id a successful create_task /
  update_task / mark_task_done identified, so a later referential
  message ("Mark it as done") can resolve "it" without asking again.
  Completely separate from _pending and _pending_confirmation -
  cancelling a pending clarification or confirmation does not touch
  this, and vice versa.

conversation_id alone is client-supplied and unauthenticated - keying
every store by (user_id, conversation_id) instead of conversation_id
alone means two different users can never share, collide on, or hijack
each other's pending state, even if they happen to reuse (or guess) the
same conversation_id. user_id always comes from the authenticated
request (see routes/agent.py), never from the client-supplied body.
"""

from dataclasses import dataclass
from uuid import UUID

ConversationKey = tuple[str, UUID]


@dataclass
class PendingClarification:
    """The minimum needed to resume an incomplete tool decision."""

    selected_tool: str
    arguments: dict[str, str | int | bool | None]
    reason: str
    missing: list[str]


_pending: dict[ConversationKey, PendingClarification] = {}


def get(user_id: str, conversation_id: UUID) -> PendingClarification | None:
    return _pending.get((user_id, conversation_id))


def set(user_id: str, conversation_id: UUID, pending: PendingClarification) -> None:
    _pending[(user_id, conversation_id)] = pending


def clear(user_id: str, conversation_id: UUID) -> None:
    _pending.pop((user_id, conversation_id), None)


@dataclass
class PendingConfirmation:
    """A fully-formed, destructive tool decision awaiting explicit confirmation."""

    selected_tool: str
    arguments: dict[str, str | int | bool | None]
    reason: str
    question: str


_pending_confirmation: dict[ConversationKey, PendingConfirmation] = {}


def get_confirmation(user_id: str, conversation_id: UUID) -> PendingConfirmation | None:
    return _pending_confirmation.get((user_id, conversation_id))


def set_confirmation(user_id: str, conversation_id: UUID, pending: PendingConfirmation) -> None:
    _pending_confirmation[(user_id, conversation_id)] = pending


def clear_confirmation(user_id: str, conversation_id: UUID) -> None:
    _pending_confirmation.pop((user_id, conversation_id), None)


_last_task_id: dict[ConversationKey, int] = {}

# Tools whose successful result unambiguously identifies exactly one
# task - worth remembering as "the task the user was just talking about".
_TASK_IDENTIFYING_TOOLS = {"create_task", "update_task", "mark_task_done"}


def get_last_task_id(user_id: str, conversation_id: UUID) -> int | None:
    return _last_task_id.get((user_id, conversation_id))


def record_result(user_id: str, conversation_id: UUID, selected_tool: str | None, result: object) -> None:
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

    key = (user_id, conversation_id)

    if selected_tool in _TASK_IDENTIFYING_TOOLS:
        task_id = result.get("id")
        if isinstance(task_id, int):
            _last_task_id[key] = task_id
    elif selected_tool == "delete_task":
        deleted_id = result.get("task_id")
        if isinstance(deleted_id, int) and _last_task_id.get(key) == deleted_id:
            _last_task_id.pop(key, None)
