"""tasks (Action Tracker)

Adds the `tasks` table behind the Action Tracker. `status` and `urgency` are plain strings (their
values are the enum values, validated by the Pydantic schema) so this migration creates no new
native enum type. `classification` reuses the existing `classification` enum — with
`create_type=False`, because that type already exists (0001) and a second `CREATE TYPE` would fail
with `duplicate_object` (see 0001, note 2).

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("urgency", sa.String(length=20), nullable=False, server_default="medium"),
        sa.Column("owner", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column(
            "classification",
            postgresql.ENUM(name="classification", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "source_insight_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("insights.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_urgency", "tasks", ["urgency"])
    op.create_index("ix_tasks_classification", "tasks", ["classification"])
    op.create_index("ix_tasks_created_by", "tasks", ["created_by"])
    op.create_index("ix_tasks_source_insight_id", "tasks", ["source_insight_id"])


def downgrade() -> None:
    op.drop_table("tasks")
