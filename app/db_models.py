"""SQLAlchemy ORM models.

Kept separate from app/schemas.py (the Pydantic request/response
shapes) so how tasks are stored can evolve independently of what the
API accepts/returns.
"""

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
