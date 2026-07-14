"""The command centre (§7.7 #21) — one call that fills the whole dashboard.

Two things govern every query in this file.

**A count is an information leak.** A viewer whose dashboard says "insights: 47" while they can
open only 12 has just been told that 35 things exist which they are not cleared to know exist.
So every table that carries a `classification` column is filtered by the caller's clearance in
SQL, on the counts as much as on the rows. The same rule reaches the approvals counter, which has
no classification of its own: it is joined to its subject so a pending OFFICIAL-SENSITIVE briefing
never shows up in a viewer's badge.

**The dashboard is the most-hit endpoint in the product.** Every counter on a table is therefore
gathered by ONE aggregate query with `FILTER (WHERE …)` clauses, not by one query per number.
Eleven counters cost five round trips, not eleven, and the figures are all read at the same
instant — a dashboard whose counters disagree with each other is worse than a slow one.

`kpi_snapshot()` is the same data the Briefing Agent reads through `get_kpi_snapshot`, so the
briefing and the UI can never quote different numbers for the same day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import structlog
from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings, get_settings
from app.enums import (
    ApprovalStatus,
    ApprovalSubject,
    Classification,
    DocumentStatus,
    InsightKind,
    ItemStatus,
    PublicationStatus,
    Urgency,
    classification_at_or_below,
)
from app.models import (
    Approval,
    Briefing,
    Document,
    IngestedItem,
    Insight,
    MemoryEntry,
    PipelineRun,
    Source,
    User,
)
from app.schemas.briefings import BriefingOut
from app.schemas.dashboard import (
    CostSummary,
    DashboardCounts,
    DashboardSummary,
    KpiSnapshot,
    SourceHealth,
)
from app.schemas.insights import InsightOut
from app.schemas.pipeline import PipelineRunOut
from app.security.rbac import user_clearance
from app.services.ingestion.runner import source_health
from app.services.llm.usage_tracker import cost_since, today_cost_usd, tokens_today

log = structlog.get_logger(__name__)

# "Recent" everywhere on this dashboard means the last 24 hours — the pipeline's daily cadence.
RECENT_WINDOW_HOURS: Final[int] = 24

# The cost panel shows today against the last week.
COST_WINDOW_DAYS: Final[int] = 7

TOP_INSIGHTS: Final[int] = 5
TOP_DOMAINS: Final[int] = 5

# "Open" = awaiting a decision or already published. A rejected insight is not an open risk; it is
# a risk a human looked at and dismissed, and counting it would make the dashboard argue with the
# person who dismissed it.
OPEN_STATUSES: Final[tuple[PublicationStatus, ...]] = (
    PublicationStatus.PENDING_APPROVAL,
    PublicationStatus.PUBLISHED,
)

# Urgencies that count as "high urgency" on the KPI row.
HIGH_URGENCIES: Final[tuple[str, ...]] = (Urgency.HIGH.value, Urgency.CRITICAL.value)


@dataclass(frozen=True, slots=True)
class _ItemCounts:
    total: int
    new: int
    triaged: int
    last_24h: int
    high_urgency_24h: int


@dataclass(frozen=True, slots=True)
class _InsightCounts:
    total: int
    published: int
    risks_open: int
    opportunities_open: int
    policy_open: int
    avg_confidence: float | None


async def kpi_snapshot(session: AsyncSession, *, clearance: Classification) -> KpiSnapshot:
    """Headline figures at this clearance. Read by the UI's KPI row and by the Briefing Agent."""
    allowed = classification_at_or_below(clearance)

    items = await _item_counts(session, allowed)
    insights = await _insight_counts(session, allowed)
    domains = await _top_domains(session, allowed)
    sources = await _active_sources(session)

    return _build_kpi(items=items, insights=insights, domains=domains, active_sources=sources)


async def dashboard_summary(
    session: AsyncSession,
    *,
    user: User,
    settings: Settings | None = None,
) -> DashboardSummary:
    """Everything the command centre renders, at the caller's clearance, in one response."""
    cfg = settings or get_settings()
    clearance = user_clearance(user)
    allowed = classification_at_or_below(clearance)

    items = await _item_counts(session, allowed)
    insights = await _insight_counts(session, allowed)
    domains = await _top_domains(session, allowed)
    active_sources = await _active_sources(session)

    counts = DashboardCounts(
        items_total=items.total,
        items_new=items.new,
        items_triaged=items.triaged,
        insights_total=insights.total,
        insights_published=insights.published,
        risks_open=insights.risks_open,
        opportunities_open=insights.opportunities_open,
        policy_open=insights.policy_open,
        approvals_pending=await _pending_approvals(session, allowed),
        documents_indexed=await _indexed_documents(session, allowed),
        memory_entries=await _memory_entries(session, allowed),
    )

    summary = DashboardSummary(
        counts=counts,
        latest_run=await _latest_run(session),
        latest_briefing=await _latest_briefing(session, allowed),
        top_insights=await _top_insights(session, clearance=clearance, allowed=allowed),
        source_health=await _source_health(session, allowed),
        cost=await _cost(session, cfg),
        kpi=_build_kpi(
            items=items,
            insights=insights,
            domains=domains,
            active_sources=active_sources,
        ),
        generated_at=datetime.now(UTC),
    )

    log.info(
        "dashboard_summary",
        user_id=str(user.id),
        role=user.role.value,
        clearance=clearance.value,
        items_total=counts.items_total,
        insights_total=counts.insights_total,
        approvals_pending=counts.approvals_pending,
    )
    return summary


# ---------------------------------------------------------------------------
# Counters — one aggregate query per table
# ---------------------------------------------------------------------------
async def _item_counts(session: AsyncSession, allowed: list[Classification]) -> _ItemCounts:
    """Total / new / triaged / last-24h / high-urgency, in a single pass over `ingested_items`.

    "Recent" is measured on `created_at` (when DANAH ingested the item), not `published_at`: the
    dashboard is answering "what has arrived since yesterday", and a source that back-fills a
    week-old report is still news to the ministry today.
    """
    recent = IngestedItem.created_at >= _since(hours=RECENT_WINDOW_HOURS)

    row = (
        await session.execute(
            select(
                func.count(IngestedItem.id),
                func.count(IngestedItem.id).filter(IngestedItem.status == ItemStatus.NEW),
                func.count(IngestedItem.id).filter(IngestedItem.status == ItemStatus.TRIAGED),
                func.count(IngestedItem.id).filter(recent),
                func.count(IngestedItem.id).filter(
                    recent, IngestedItem.triage["urgency"].astext.in_(HIGH_URGENCIES)
                ),
            ).where(IngestedItem.classification.in_(allowed))
        )
    ).one()

    return _ItemCounts(
        total=int(row[0] or 0),
        new=int(row[1] or 0),
        triaged=int(row[2] or 0),
        last_24h=int(row[3] or 0),
        high_urgency_24h=int(row[4] or 0),
    )


async def _insight_counts(session: AsyncSession, allowed: list[Classification]) -> _InsightCounts:
    """Insight totals plus the three open-by-kind counters, in a single pass over `insights`."""
    is_open = Insight.status.in_(OPEN_STATUSES)

    row = (
        await session.execute(
            select(
                func.count(Insight.id),
                func.count(Insight.id).filter(Insight.status == PublicationStatus.PUBLISHED),
                func.count(Insight.id).filter(Insight.kind == InsightKind.RISK, is_open),
                func.count(Insight.id).filter(Insight.kind == InsightKind.OPPORTUNITY, is_open),
                func.count(Insight.id).filter(Insight.kind == InsightKind.POLICY, is_open),
                # Rejected analyses are excluded: a number a human threw out should not drag down
                # the confidence the dashboard reports for the work that survived.
                func.avg(Insight.confidence).filter(Insight.status != PublicationStatus.REJECTED),
            ).where(Insight.classification.in_(allowed))
        )
    ).one()

    average = row[5]
    return _InsightCounts(
        total=int(row[0] or 0),
        published=int(row[1] or 0),
        risks_open=int(row[2] or 0),
        opportunities_open=int(row[3] or 0),
        policy_open=int(row[4] or 0),
        avg_confidence=round(float(average), 3) if average is not None else None,
    )


async def _top_domains(session: AsyncSession, allowed: list[Classification]) -> list[str]:
    """The domains the analysis is actually landing in, most active first.

    `domains` is a `text[]`, so the inner select `unnest`s it into one row per domain — with the
    clearance filter already applied — and the outer query groups those rows. Counting in SQL
    keeps this to one round trip instead of dragging every insight into Python to tally it.
    """
    expanded = (
        select(func.unnest(Insight.domains).label("domain"))
        .where(
            Insight.classification.in_(allowed),
            Insight.status.in_(OPEN_STATUSES),
        )
        .subquery()
    )

    stmt = (
        select(expanded.c.domain, func.count().label("hits"))
        .group_by(expanded.c.domain)
        .order_by(func.count().desc())
        .limit(TOP_DOMAINS)
    )

    rows = (await session.execute(stmt)).all()
    return [str(name) for name, _hits in rows]


async def _active_sources(session: AsyncSession) -> int:
    """Sources carry no classification — a feed's existence is not itself sensitive."""
    total = await session.scalar(
        select(func.count(Source.id)).select_from(Source).where(Source.enabled.is_(True))
    )
    return int(total or 0)


async def _pending_approvals(session: AsyncSession, allowed: list[Classification]) -> int:
    """Pending approvals whose *subject* this caller is cleared to see.

    `approvals` has no classification column, so the clearance filter reaches it through the
    subject it points at. Without this join, a viewer's badge would count a pending
    OFFICIAL-SENSITIVE briefing — announcing the existence of something they may not read.
    """
    total = await session.scalar(
        select(func.count(Approval.id))
        .select_from(Approval)
        .outerjoin(Insight, _insight_subject_join())
        .outerjoin(Briefing, _briefing_subject_join())
        .where(
            Approval.status == ApprovalStatus.PENDING,
            or_(
                Insight.classification.in_(allowed),
                Briefing.classification.in_(allowed),
            ),
        )
    )
    return int(total or 0)


async def _indexed_documents(session: AsyncSession, allowed: list[Classification]) -> int:
    total = await session.scalar(
        select(func.count(Document.id))
        .select_from(Document)
        .where(
            Document.status == DocumentStatus.INDEXED,
            Document.classification.in_(allowed),
        )
    )
    return int(total or 0)


async def _memory_entries(session: AsyncSession, allowed: list[Classification]) -> int:
    total = await session.scalar(
        select(func.count(MemoryEntry.id))
        .select_from(MemoryEntry)
        .where(MemoryEntry.classification.in_(allowed))
    )
    return int(total or 0)


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------
async def _latest_run(session: AsyncSession) -> PipelineRunOut | None:
    """The most recent pipeline run, with its steps eagerly loaded for the roll-ups.

    `selectinload` is not optional here: `PipelineRun.total_tokens` walks `run.steps`, and a lazy
    load on an async session raises `MissingGreenlet` rather than quietly working.
    """
    run = await session.scalar(
        select(PipelineRun)
        .options(selectinload(PipelineRun.steps))
        .order_by(PipelineRun.started_at.desc())
        .limit(1)
    )
    if run is None:
        return None

    duration_ms: int | None = None
    if run.finished_at is not None:
        duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)

    return PipelineRunOut(
        id=run.id,
        trigger=run.trigger,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        stats=run.stats,
        initiated_by=run.initiated_by,
        total_tokens=run.total_tokens,
        total_cost_usd=float(run.total_cost_usd),
        duration_ms=duration_ms,
        step_count=len(run.steps),
    )


async def _latest_briefing(
    session: AsyncSession, allowed: list[Classification]
) -> BriefingOut | None:
    """The newest *published* briefing this caller may read.

    Unapproved briefings are deliberately absent: a draft's only route to a reader is the approval
    queue, and surfacing one here would be a second, unguarded route to the same content.
    """
    briefing = await session.scalar(
        select(Briefing)
        .where(
            Briefing.status == PublicationStatus.PUBLISHED,
            Briefing.classification.in_(allowed),
        )
        .order_by(Briefing.date.desc(), Briefing.created_at.desc())
        .limit(1)
    )
    return BriefingOut.model_validate(briefing) if briefing is not None else None


async def _top_insights(
    session: AsyncSession,
    *,
    clearance: Classification,
    allowed: list[Classification],
) -> list[InsightOut]:
    """The five most severe live insights, most confident first within a severity band."""
    insights = list(
        (
            await session.scalars(
                select(Insight)
                .where(
                    Insight.classification.in_(allowed),
                    Insight.status.in_(OPEN_STATUSES),
                )
                .order_by(
                    Insight.severity.desc(),
                    Insight.confidence.desc(),
                    Insight.created_at.desc(),
                )
                .limit(TOP_INSIGHTS)
            )
        ).all()
    )
    if not insights:
        return []

    # Imported here, not at module scope: the citation resolver belongs to the insights endpoint
    # that owns the JSONB shape (one resolver, so the dashboard and `GET /api/insights` can never
    # number the same citations differently), and this keeps the API layer out of the worker's
    # import graph, which reaches this module through the agents' `get_kpi_snapshot` tool.
    from app.api.insights import insights_out

    return await insights_out(session, insights, clearance=clearance)


async def _source_health(
    session: AsyncSession, allowed: list[Classification]
) -> list[SourceHealth]:
    """Every source with its traffic light and its 24-hour yield.

    The per-source item count is a grouped sub-select joined once, not a count per source.
    """
    recent_items = (
        select(
            IngestedItem.source_id.label("source_id"),
            func.count(IngestedItem.id).label("items"),
        )
        .where(
            IngestedItem.created_at >= _since(hours=RECENT_WINDOW_HOURS),
            IngestedItem.classification.in_(allowed),
        )
        .group_by(IngestedItem.source_id)
        .subquery()
    )

    rows = (
        await session.execute(
            # `recent_items.c["items"]`, not `.c.items`: ColumnCollection is dict-like, so the
            # attribute resolves to the bound `.items()` method and silently shadows the column
            # labelled "items". SQLAlchemy then passes the method into coalesce() as a bind
            # parameter and asyncpg rejects it at execution time — this endpoint 500'd for every
            # caller. The label is only reachable by subscript.
            select(Source, func.coalesce(recent_items.c["items"], 0))
            .outerjoin(recent_items, recent_items.c.source_id == Source.id)
            .order_by(Source.name)
        )
    ).all()

    return [
        SourceHealth(
            id=source.id,
            name=source.name,
            connector=source.connector.value,
            enabled=source.enabled,
            # The ingestion runner owns the definition of "stale" and "failing"; the dashboard
            # renders its verdict rather than inventing a second one that could disagree.
            health=await source_health(source),
            last_synced_at=source.last_synced_at,
            last_status=source.last_status,
            items_last_24h=int(items or 0),
        )
        for source, items in rows
    ]


async def _cost(session: AsyncSession, settings: Settings) -> CostSummary:
    today = float(await today_cost_usd(session))
    threshold = settings.daily_cost_alert_usd

    return CostSummary(
        today_usd=round(today, 4),
        last_7d_usd=round(float(await cost_since(session, days=COST_WINDOW_DAYS)), 4),
        tokens_today=await tokens_today(session),
        daily_alert_threshold_usd=threshold,
        # At the threshold, not merely past it: the alert exists to be seen before the budget is
        # gone, and a threshold of 0 disables it rather than firing on the first token.
        over_threshold=threshold > 0 and today >= threshold,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _build_kpi(
    *,
    items: _ItemCounts,
    insights: _InsightCounts,
    domains: list[str],
    active_sources: int,
) -> KpiSnapshot:
    """Assemble the KPI row from aggregates that were already read — no second round trip."""
    return KpiSnapshot(
        generated_at=datetime.now(UTC),
        items_last_24h=items.last_24h,
        high_urgency_items=items.high_urgency_24h,
        avg_insight_confidence=insights.avg_confidence,
        top_domains=domains,
        active_sources=active_sources,
    )


def _insight_subject_join() -> ColumnElement[bool]:
    return and_(
        Approval.subject_type == ApprovalSubject.INSIGHT,
        Insight.id == Approval.subject_id,
    )


def _briefing_subject_join() -> ColumnElement[bool]:
    return and_(
        Approval.subject_type == ApprovalSubject.BRIEFING,
        Briefing.id == Approval.subject_id,
    )


def _since(*, hours: int) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)
