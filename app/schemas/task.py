"""Action Tracker schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

from pydantic import Field

from app.enums import Classification, TaskStatus, Urgency
from app.schemas.common import DanahModel


class TaskOut(DanahModel):
    id: uuid.UUID
    title: str
    description: str
    status: TaskStatus
    urgency: Urgency
    owner: str
    progress: int
    due_date: date | None = None
    classification: Classification
    created_by: uuid.UUID | None = None
    source_insight_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class TaskCreate(DanahModel):
    title: Annotated[str, Field(min_length=1, max_length=500)]
    description: Annotated[str, Field(max_length=8000)] = ""
    urgency: Urgency = Urgency.MEDIUM
    owner: Annotated[str, Field(max_length=200)] = ""
    due_date: date | None = None
    classification: Classification = Classification.OFFICIAL
    source_insight_id: uuid.UUID | None = None


class TaskUpdate(DanahModel):
    title: Annotated[str | None, Field(default=None, max_length=500)] = None
    description: Annotated[str | None, Field(default=None, max_length=8000)] = None
    status: TaskStatus | None = None
    urgency: Urgency | None = None
    owner: Annotated[str | None, Field(default=None, max_length=200)] = None
    progress: Annotated[int | None, Field(default=None, ge=0, le=100)] = None
    due_date: date | None = None
