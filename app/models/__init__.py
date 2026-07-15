"""SQLAlchemy models.

Every model is imported here so that `Base.metadata` is complete by the time Alembic
autogenerate or the test harness inspects it.
"""

from __future__ import annotations

from app.db import Base
from app.models.approval import Approval
from app.models.audit import AuditLog
from app.models.briefing import Briefing
from app.models.chat import ChatMessage, ChatSession
from app.models.document import Document, DocumentChunk
from app.models.insight import Insight
from app.models.memory import MemoryEntry
from app.models.notification import Notification
from app.models.pipeline import PipelineRun, PipelineStep
from app.models.source import IngestedItem, Source
from app.models.task import Task
from app.models.usage import ApiUsage
from app.models.user import RefreshToken, User

__all__ = [
    "ApiUsage",
    "Approval",
    "AuditLog",
    "Base",
    "Briefing",
    "ChatMessage",
    "ChatSession",
    "Document",
    "DocumentChunk",
    "IngestedItem",
    "Insight",
    "MemoryEntry",
    "Notification",
    "PipelineRun",
    "PipelineStep",
    "RefreshToken",
    "Source",
    "Task",
    "User",
]
