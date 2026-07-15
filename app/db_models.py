"""SQLAlchemy ORM models.

Kept separate from app/schemas.py (the Pydantic request/response
shapes) so how tasks are stored can evolve independently of what the
API accepts/returns.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
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
