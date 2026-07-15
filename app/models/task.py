"""Action Tracker — decisions turned into owned, tracked tasks (the prototype's Action Tracker,
made real). Clearance-filtered like every other read in DANAH.

`status` and `urgency` are stored as validated strings, not native pg enums: their values are the
enum values (`pending`, `medium`, …) and the Pydantic schema is the guard, which keeps this
follow-on migration free of the pg-enum-type churn that a new native enum would add. `classification`
reuses the existing native enum so the same SQL clearance filter used everywhere else applies here.
"""

from __future__ import annotations

import uuid
from datetime import date

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.enums import Classification
from app.models.base import pg_enum


class Task(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tasks"

    title: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    description: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="", server_default=""
    )
    status: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, default="pending", server_default="pending", index=True
    )
    urgency: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, default="medium", server_default="medium", index=True
    )
    owner: Mapped[str] = mapped_column(
        sa.String(200), nullable=False, default="", server_default=""
    )
    progress: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default="0"
    )
    due_date: Mapped[date | None] = mapped_column(sa.Date)
    classification: Mapped[Classification] = mapped_column(
        pg_enum(Classification, "classification"),
        nullable=False,
        default=Classification.OFFICIAL,
        index=True,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    # An action created from an approved insight keeps the link back to it.
    source_insight_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("insights.id", ondelete="SET NULL"), index=True
    )
