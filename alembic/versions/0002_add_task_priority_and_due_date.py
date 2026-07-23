"""add task priority and due_date

Adds two additive, nullable-or-defaulted columns to the existing `tasks`
table - never drops or recreates it. Both new columns match
app/db_models.py's Task model exactly (verified via
tests/test_alembic_migrations.py::test_baseline_matches_current_orm_metadata's
`alembic check`, the same drift-detection this project already relies
on for 0001_baseline).

- priority: NOT NULL with server_default='medium', so the single
  `ADD COLUMN ... DEFAULT 'medium'` statement backfills every
  pre-existing row atomically, and any raw INSERT (bypassing the ORM's
  own Python-side default) still satisfies the NOT NULL constraint.
- due_date: nullable, no default needed - a pre-existing row simply has
  no due date, which is exactly what "optional" already means here.

Downgrade uses `op.batch_alter_table` (not a bare `op.drop_column`)
specifically for SQLite: older/some SQLite builds can't run DROP COLUMN
directly at all, and batch mode is the standard Alembic-recommended,
cross-dialect-safe way to handle that - it transparently falls back to
a safe "create new table, copy rows, swap" sequence only where the
dialect actually requires it (SQLite), and uses a plain ALTER TABLE
DROP COLUMN directly on backends that support it natively
(PostgreSQL). Either way, all existing rows/ids and the other columns
(id, user_id, title, description, done) are preserved untouched -
verified in tests/test_alembic_migrations.py against temporary
databases only.

Revision ID: 0002_add_task_priority_and_due_date
Revises: 0001_baseline
Create Date: 2026-07-23

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_add_task_priority_and_due_date"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("priority", sa.String(), nullable=False, server_default="medium"))
    op.add_column("tasks", sa.Column("due_date", sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("due_date")
        batch_op.drop_column("priority")
