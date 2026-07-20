"""SQLAlchemy ORM models.

Kept separate from app/schemas.py (the Pydantic request/response
shapes) so how tasks are stored can evolve independently of what the
API accepts/returns.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class AgentRun(Base):
    """One persistent trace per POST /agent/execute HTTP request.

    Every request gets its own run_id, even a follow-up reply ("yes", a
    clarification answer) on the same conversation_id - runs are never
    merged across requests, only linked via conversation_id.
    """

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True, index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    message: Mapped[str] = mapped_column(String, nullable=False)
    decision_provider: Mapped[str] = mapped_column(String, nullable=False)
    is_multi_step: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    selected_tool: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    error: Mapped[str | None] = mapped_column(String, nullable=True)

    steps: Mapped[list["AgentRunStep"]] = relationship(
        back_populates="run", order_by="AgentRunStep.step_number", cascade="all, delete-orphan"
    )


class AgentRunStep(Base):
    """One attempted tool call within an AgentRun - one row for a
    single-step execution's tool (when a tool actually ran), or one row
    per attempted step (success, error, or stopped) for a multi-step plan.
    """

    __tablename__ = "agent_run_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_runs.run_id"), nullable=False, index=True)
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    tool: Mapped[str] = mapped_column(String, nullable=False)
    arguments_json: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    # Bounded, JSON-safe summary of the step's result (see
    # agent_trace_service._summarize_result) - never a raw, potentially
    # large external API response.
    result_summary: Mapped[str | None] = mapped_column(String, nullable=True)

    run: Mapped["AgentRun"] = relationship(back_populates="steps")


class ConversationState(Base):
    """Durable replacement for conversation_memory's three in-process
    dicts - one row per (user_id, conversation_id), holding three
    independent, nullable "slots": a pending clarification, a pending
    destructive-action confirmation, and a remembered last_task_id.

    A row can have any subset of the three slots populated (a
    clarification and a confirmation are never both pending at once in
    practice, but nothing here enforces that - see conversation_memory.py
    for the actual state machine). Each slot has its own *_expires_at
    timestamp; an expired slot must be treated as absent by every reader
    (see conversation_memory.py's get/peek functions), never as valid
    just because the row still physically exists.

    Reused across restarts because it's just another table in the same
    SQLite database as Task/AgentRun - no separate engine, no separate
    volume.
    """

    __tablename__ = "conversation_states"
    __table_args__ = (
        UniqueConstraint("user_id", "conversation_id", name="uq_conversation_states_user_conversation"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)

    # Pending clarification slot (mirrors conversation_memory.PendingClarification).
    clarification_tool: Mapped[str | None] = mapped_column(String, nullable=True)
    clarification_arguments_json: Mapped[str | None] = mapped_column(String, nullable=True)
    clarification_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    clarification_missing_json: Mapped[str | None] = mapped_column(String, nullable=True)
    clarification_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Pending confirmation slot (mirrors conversation_memory.PendingConfirmation).
    confirmation_tool: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmation_arguments_json: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmation_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmation_question: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmation_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Remembered context slot ("it"/"that one" resolution).
    last_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
