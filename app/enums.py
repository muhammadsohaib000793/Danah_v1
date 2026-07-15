"""Domain enumerations shared by SQLAlchemy models, Pydantic schemas and services.

These names are the wire contract: their *values* appear in API responses and in the
database, so they are lower-case strings and must not be renamed without a migration
and a `docs/API.md` changelog entry.
"""

from __future__ import annotations

from enum import StrEnum


class Classification(StrEnum):
    """Data sensitivity tier. Ordered: PUBLIC < INTERNAL < OFFICIAL < OFFICIAL_SENSITIVE."""

    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    OFFICIAL = "OFFICIAL"
    OFFICIAL_SENSITIVE = "OFFICIAL_SENSITIVE"


# Numeric rank used for clearance comparisons. Higher = more sensitive.
CLASSIFICATION_RANK: dict[Classification, int] = {
    Classification.PUBLIC: 0,
    Classification.INTERNAL: 1,
    Classification.OFFICIAL: 2,
    Classification.OFFICIAL_SENSITIVE: 3,
}


def classification_at_or_below(ceiling: Classification) -> list[Classification]:
    """Every classification a holder of `ceiling` clearance is allowed to read."""
    limit = CLASSIFICATION_RANK[ceiling]
    return [c for c, rank in CLASSIFICATION_RANK.items() if rank <= limit]


class Role(StrEnum):
    """User role. Clearance ceiling per role lives in `Settings.role_clearance`."""

    ADMIN = "admin"
    EXECUTIVE = "executive"
    ANALYST = "analyst"
    VIEWER = "viewer"


class Language(StrEnum):
    EN = "en"
    AR = "ar"


class DocumentStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"


class SourceType(StrEnum):
    API = "api"
    RSS = "rss"
    WEBHOOK = "webhook"
    MANUAL = "manual"


class ConnectorKind(StrEnum):
    WORLDBANK = "worldbank"
    GDELT = "gdelt"
    RSS = "rss"
    RELIEFWEB = "reliefweb"
    CUSTOM = "custom"


class ItemStatus(StrEnum):
    NEW = "new"
    TRIAGED = "triaged"
    ANALYZED = "analyzed"
    ARCHIVED = "archived"


class ItemCategory(StrEnum):
    """Signal Agent triage category."""

    ECONOMIC = "economic"
    GEOPOLITICAL = "geopolitical"
    REGULATORY = "regulatory"
    TECHNOLOGY = "technology"
    SOCIAL = "social"


class Urgency(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TaskStatus(StrEnum):
    """Lifecycle of a tracked action (the Action Tracker)."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"


class PipelineTrigger(StrEnum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


class StepStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class AgentName(StrEnum):
    """The six agents. Value is persisted in `pipeline_steps.agent`."""

    SIGNAL = "signal"
    RISK = "risk"
    OPPORTUNITY = "opportunity"
    POLICY = "policy"
    BRIEFING = "briefing"
    MEMORY = "memory"


class InsightKind(StrEnum):
    RISK = "risk"
    OPPORTUNITY = "opportunity"
    POLICY = "policy"


class PublicationStatus(StrEnum):
    """Lifecycle of anything an agent produces. Only a human decision reaches `published`."""

    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    PUBLISHED = "published"
    REJECTED = "rejected"


class ApprovalSubject(StrEnum):
    INSIGHT = "insight"
    BRIEFING = "briefing"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"


class MemoryKind(StrEnum):
    DECISION = "decision"
    LESSON = "lesson"
    CONTEXT = "context"


class ChatRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ActorType(StrEnum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class UsagePurpose(StrEnum):
    CHAT = "chat"
    AGENT = "agent"
    EMBEDDING = "embedding"


class NotificationKind(StrEnum):
    APPROVAL_PENDING = "approval_pending"
    BRIEFING_PUBLISHED = "briefing_published"
    COST_ALERT = "cost_alert"
    SOURCE_FAILURE = "source_failure"


class LLMProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class EmbeddingProvider(StrEnum):
    VOYAGE = "voyage"
    OPENAI = "openai"


class StorageBackend(StrEnum):
    LOCAL = "local"
    S3 = "s3"


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
