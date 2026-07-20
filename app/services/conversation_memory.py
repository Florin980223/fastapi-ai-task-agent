"""SQLite-backed storage for pending multi-turn clarifications, pending
destructive-action confirmations, and remembered conversation context.

One row per (user_id, conversation_id) in the ConversationState table
(app/db_models.py), with three independent, nullable "slots":

- clarification: when POST /agent/execute can't complete a tool decision
  because a required argument is missing, the decision is parked here
  so the next request in the same conversation can supply just the
  missing piece (e.g. "3") instead of restating the whole request.
- confirmation: when a decision is complete but selects a destructive
  tool (e.g. delete_task), it is parked here instead of being executed,
  so the next request in the same conversation must explicitly confirm
  ("yes") or cancel ("no") it. Completely separate from clarification -
  a conversation is only ever waiting on one of the two at a time, but
  clearing/resolving one never touches the other.
- context (last_task_id): the most recent task id a successful
  create_task/update_task/mark_task_done identified, so a later
  referential message ("Mark it as done") can resolve "it" without
  asking again.

Each slot has its own expiration timestamp (CONFIRMATION_TTL_SECONDS /
CLARIFICATION_TTL_SECONDS / CONTEXT_TTL_SECONDS, app/config.py). An
expired slot must be treated as absent by every reader, even though the
row may still physically exist.

conversation_id alone is client-supplied and unauthenticated - every
function is keyed by (user_id, conversation_id) so two different users
can never share, collide on, or hijack each other's pending state, even
if they happen to reuse (or guess) the same conversation_id. user_id
always comes from the authenticated request (see routes/agent.py),
never from the client-supplied body.

Two deliberately different write postures, matching how safety-critical
the state is:

- Clarification and confirmation writes (get/set/clear,
  peek_confirmation/set_confirmation/consume_confirmation) are
  synchronous, using the request's own injected db: Session, and commit
  inline before the caller may report needs_clarification/needs_
  confirmation=true. A commit failure here is never swallowed - it
  propagates so routes/agent.py can turn it into a safe generic error
  instead of ever claiming pending state that isn't actually durable.
- record_result (last_task_id) is best-effort, following the exact
  isolation idiom used by agent_trace_service.record_execute_run: a
  brand new database.SessionLocal() session, wrapped in try/except/
  rollback/finally-close, never raising. By the time it runs, the
  task mutation it's recording context for has already been committed
  on the request's own session, so a failure here can never undo it -
  it only affects whether a *future* message can resolve "it".
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import config, database
from app.db_models import ConversationState

logger = logging.getLogger(__name__)

_CLARIFICATION_TTL = timedelta(seconds=config.CLARIFICATION_TTL_SECONDS)
_CONFIRMATION_TTL = timedelta(seconds=config.CONFIRMATION_TTL_SECONDS)
_CONTEXT_TTL = timedelta(seconds=config.CONTEXT_TTL_SECONDS)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_safe(value: object) -> str:
    """Serialize a value already known to be tool-schema-shaped
    (dict/list/str/int/bool/None) into a JSON string, routed through
    jsonable_encoder first - same idiom as agent_trace_service._json_safe.
    """
    return json.dumps(jsonable_encoder(value))


@dataclass
class PendingClarification:
    """The minimum needed to resume an incomplete tool decision."""

    selected_tool: str
    arguments: dict[str, str | int | bool | None]
    reason: str
    missing: list[str]


@dataclass
class PendingConfirmation:
    """A fully-formed, destructive tool decision awaiting explicit confirmation."""

    selected_tool: str
    arguments: dict[str, str | int | bool | None]
    reason: str
    question: str


# Tools whose successful result unambiguously identifies exactly one
# task - worth remembering as "the task the user was just talking about".
_TASK_IDENTIFYING_TOOLS = {"create_task", "update_task", "mark_task_done"}


def _get_row(db: Session, user_id: str, conversation_id: UUID) -> ConversationState | None:
    stmt = select(ConversationState).where(
        ConversationState.user_id == user_id, ConversationState.conversation_id == conversation_id
    )
    return db.scalar(stmt)


def _get_or_create_row(db: Session, user_id: str, conversation_id: UUID) -> ConversationState:
    row = _get_row(db, user_id, conversation_id)
    if row is not None:
        return row
    now = _now()
    row = ConversationState(user_id=user_id, conversation_id=conversation_id, created_at=now, updated_at=now)
    db.add(row)
    db.flush()
    return row


def _not_expired(expires_at: datetime | None) -> bool:
    """True if expires_at is set and still in the future.

    SQLite has no native tz-aware datetime type: SQLAlchemy's
    DateTime(timezone=True) silently strips tzinfo on write and always
    hands back a naive datetime on read, regardless of that flag (see
    https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#date-and-time-types).
    Every datetime this module ever writes is UTC (via _now()), so a
    naive value read back is always implicitly UTC - reattach that
    tzinfo before comparing against the tz-aware _now(), or this raises
    TypeError: can't compare offset-naive and offset-aware datetimes.
    """
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > _now()


# --- Clarification slot -----------------------------------------------------


def get(db: Session, user_id: str, conversation_id: UUID) -> PendingClarification | None:
    row = _get_row(db, user_id, conversation_id)
    if row is None or row.clarification_tool is None or not _not_expired(row.clarification_expires_at):
        return None
    return PendingClarification(
        selected_tool=row.clarification_tool,
        arguments=json.loads(row.clarification_arguments_json),
        reason=row.clarification_reason,
        missing=json.loads(row.clarification_missing_json),
    )


def set(db: Session, user_id: str, conversation_id: UUID, pending: PendingClarification) -> None:
    """Park (or overwrite) the pending clarification for this conversation.

    Commits synchronously - correctness-critical (see module docstring).
    Raises on failure rather than swallowing it; callers must not report
    needs_clarification=true unless this returns successfully.
    """
    now = _now()
    row = _get_or_create_row(db, user_id, conversation_id)
    row.clarification_tool = pending.selected_tool
    row.clarification_arguments_json = _json_safe(pending.arguments)
    row.clarification_reason = pending.reason
    row.clarification_missing_json = json.dumps(pending.missing)
    row.clarification_expires_at = now + _CLARIFICATION_TTL
    row.updated_at = now
    db.commit()


def clear(db: Session, user_id: str, conversation_id: UUID) -> None:
    """Clear any pending clarification for this conversation. A no-op
    (not an error) if there was nothing pending. Commits synchronously.
    """
    row = _get_row(db, user_id, conversation_id)
    if row is None or row.clarification_tool is None:
        return
    row.clarification_tool = None
    row.clarification_arguments_json = None
    row.clarification_reason = None
    row.clarification_missing_json = None
    row.clarification_expires_at = None
    row.updated_at = _now()
    db.commit()


# --- Confirmation slot -------------------------------------------------------


def peek_confirmation(db: Session, user_id: str, conversation_id: UUID) -> PendingConfirmation | None:
    """Read-only look at the pending confirmation, without consuming it.
    For the cancellation and ambiguous-reply branches, which must not
    clear anything. Use consume_confirmation to actually act on a "yes".
    """
    row = _get_row(db, user_id, conversation_id)
    if row is None or row.confirmation_tool is None or not _not_expired(row.confirmation_expires_at):
        return None
    return PendingConfirmation(
        selected_tool=row.confirmation_tool,
        arguments=json.loads(row.confirmation_arguments_json),
        reason=row.confirmation_reason,
        question=row.confirmation_question,
    )


def set_confirmation(db: Session, user_id: str, conversation_id: UUID, pending: PendingConfirmation) -> None:
    """Park the pending confirmation for this conversation. Commits
    synchronously; raises on failure rather than swallowing it - callers
    must not report needs_confirmation=true unless this returns
    successfully (see module docstring).
    """
    now = _now()
    row = _get_or_create_row(db, user_id, conversation_id)
    row.confirmation_tool = pending.selected_tool
    row.confirmation_arguments_json = _json_safe(pending.arguments)
    row.confirmation_reason = pending.reason
    row.confirmation_question = pending.question
    row.confirmation_expires_at = now + _CONFIRMATION_TTL
    row.updated_at = now
    db.commit()


def clear_confirmation(db: Session, user_id: str, conversation_id: UUID) -> None:
    """Clear any pending confirmation without executing anything - used
    by the cancellation branch. Commits synchronously.
    """
    row = _get_row(db, user_id, conversation_id)
    if row is None or row.confirmation_tool is None:
        return
    row.confirmation_tool = None
    row.confirmation_arguments_json = None
    row.confirmation_reason = None
    row.confirmation_question = None
    row.confirmation_expires_at = None
    row.updated_at = _now()
    db.commit()


def consume_confirmation(db: Session, user_id: str, conversation_id: UUID) -> PendingConfirmation | None:
    """Atomically read-and-clear the pending confirmation for this
    conversation in a single conditional UPDATE, so a second or
    concurrent "yes" can never consume the same confirmation twice, and
    an expired confirmation can never be consumed at all.

    Returns the consumed PendingConfirmation, or None if there was
    nothing valid to consume (no row, already consumed, or expired).
    Callers MUST treat None exactly like "no pending confirmation exists"
    and must not execute the destructive tool - never fall back to
    executing based on a value read before calling this function.
    """
    row = _get_row(db, user_id, conversation_id)
    if row is None or row.confirmation_tool is None or not _not_expired(row.confirmation_expires_at):
        return None

    # Snapshot before clearing - discarded below if the conditional
    # UPDATE ends up matching zero rows (a concurrent request already
    # consumed/cleared it, or it expired, between this read and the
    # UPDATE's own re-check of the same conditions).
    consumed = PendingConfirmation(
        selected_tool=row.confirmation_tool,
        arguments=json.loads(row.confirmation_arguments_json),
        reason=row.confirmation_reason,
        question=row.confirmation_question,
    )

    now = _now()
    result = db.execute(
        update(ConversationState)
        .where(
            ConversationState.user_id == user_id,
            ConversationState.conversation_id == conversation_id,
            ConversationState.confirmation_tool.is_not(None),
            ConversationState.confirmation_expires_at > now,
        )
        .values(
            confirmation_tool=None,
            confirmation_arguments_json=None,
            confirmation_reason=None,
            confirmation_question=None,
            confirmation_expires_at=None,
            updated_at=now,
        )
        # The default synchronize_session="auto" re-evaluates the WHERE
        # clause in Python against any already-loaded row in this
        # Session's identity map (the _get_row call just above loaded
        # exactly that row) to keep it in sync - and that Python-side
        # evaluator hits the same naive/aware datetime comparison issue
        # as _not_expired above (confirmation_expires_at comes back from
        # SQLite as naive, `now` is tz-aware), raising TypeError. Nothing
        # here needs the ORM object refreshed - result.rowcount is the
        # only thing this function acts on - so skip that sync entirely.
        .execution_options(synchronize_session=False)
    )
    db.commit()

    if result.rowcount != 1:
        # Someone else already consumed it (or it expired) between our
        # read above and this UPDATE - the snapshot is stale, discard it.
        return None
    return consumed


# --- Context slot (last_task_id) --------------------------------------------


def get_last_task_id(db: Session, user_id: str, conversation_id: UUID) -> int | None:
    row = _get_row(db, user_id, conversation_id)
    if row is None or row.last_task_id is None or not _not_expired(row.context_expires_at):
        return None
    return row.last_task_id


def record_result(user_id: str, conversation_id: UUID, selected_tool: str | None, result: object) -> None:
    """Update remembered context based on a tool's execution result.

    Only create_task/update_task/mark_task_done set it, and only on a
    genuine success (a dict, no "error" key, an integer id). delete_task
    clears it, but only when the id it actually deleted matches the
    remembered one. list_tasks (and anything else) is never touched.

    Best-effort: opens its own database.SessionLocal() session,
    completely separate from the request's own db session, so a failure
    here can never roll back or otherwise affect an already-committed
    task mutation (see module docstring). Never raises.
    """
    if not isinstance(result, dict) or "error" in result:
        return

    trace_db: Session = database.SessionLocal()
    try:
        now = _now()
        if selected_tool in _TASK_IDENTIFYING_TOOLS:
            task_id = result.get("id")
            if isinstance(task_id, int):
                row = _get_or_create_row(trace_db, user_id, conversation_id)
                row.last_task_id = task_id
                row.context_expires_at = now + _CONTEXT_TTL
                row.updated_at = now
                trace_db.commit()
        elif selected_tool == "delete_task":
            deleted_id = result.get("task_id")
            if isinstance(deleted_id, int):
                row = _get_row(trace_db, user_id, conversation_id)
                if row is not None and row.last_task_id == deleted_id:
                    row.last_task_id = None
                    row.context_expires_at = None
                    row.updated_at = now
                    trace_db.commit()
    except Exception as exc:
        # Never expose secrets: only the opaque conversation_id and the
        # exception text are logged - same idiom as agent_trace_service.
        logger.warning(
            "Failed to persist conversation context for user_id=%s conversation_id=%s: %s",
            user_id,
            conversation_id,
            exc,
        )
        trace_db.rollback()
    finally:
        trace_db.close()
