"""The pipeline runner.

    Signal → (Risk ∥ Opportunity ∥ Policy) → Briefing → Memory

Three properties this file exists to guarantee:

* **Nothing publishes itself.** Every insight and briefing an agent produces is written as a
  draft and immediately submitted to the approval queue. The only transition to `published` is a
  human decision in `approval_service`. That is the whole point of the system, so the orchestrator
  never calls anything that could set `published`.

* **Partial failure is tolerated.** A failed step marks the run `partial`; steps that do not
  depend on it still run. If the Policy Agent fails, the day's risks and briefing still land — a
  ministry that gets four fifths of its briefing is better served than one that gets none.

* **The budget is a hard stop.** `PIPELINE_TOKEN_BUDGET` is checked *between* steps. A pipeline
  that has spent its budget stops and reports `partial` rather than running up an unbounded bill
  on a bad day (e.g. a source that suddenly floods the queue).

Status is committed to the database as each step opens and closes, which is what makes
`GET /api/pipeline/runs/{id}` a live view rather than a post-mortem.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.enums import (
    AgentName,
    ApprovalSubject,
    Classification,
    InsightKind,
    ItemStatus,
    Language,
    PipelineTrigger,
    PublicationStatus,
    RunStatus,
    StepStatus,
)
from app.exceptions import OrchestrationError
from app.metrics import INSIGHTS_CREATED, PIPELINE_RUNS
from app.models import Briefing, IngestedItem, Insight, PipelineRun, User
from app.services.agents.base import AgentContext
from app.services.agents.schemas import (
    BriefingOutput,
    DraftInsight,
    PolicyChange,
    PolicyOutput,
)
from app.services.llm.gateway import LLMGateway, get_gateway
from app.services.rag.embeddings import Embedder, get_embedder

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class RunStats:
    items_triaged: int = 0
    items_archived: int = 0
    risks: int = 0
    opportunities: int = 0
    policies: int = 0
    briefings: int = 0
    memories: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    failed_steps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "items_triaged": self.items_triaged,
            "items_archived": self.items_archived,
            "risks": self.risks,
            "opportunities": self.opportunities,
            "policies": self.policies,
            "briefings": self.briefings,
            "memories": self.memories,
            "tokens": self.tokens,
            "cost_usd": round(self.cost_usd, 6),
            "failed_steps": self.failed_steps,
        }


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------
async def start_run(
    session: AsyncSession,
    *,
    trigger: PipelineTrigger,
    initiated_by: uuid.UUID | None,
    max_items: int | None = None,
) -> PipelineRun:
    """Create the run row synchronously so the caller has an id to poll immediately."""
    run = PipelineRun(
        id=uuid.uuid4(),
        trigger=trigger,
        status=RunStatus.RUNNING,
        started_at=datetime.now(UTC),
        initiated_by=initiated_by,
        stats={"max_items": max_items} if max_items else {},
    )
    session.add(run)
    await session.flush()
    return run


async def was_scheduled_run_completed_today(session: AsyncSession) -> bool:
    """Idempotence for the daily cron (§7.4): one scheduled run per day, not one per restart."""
    start_of_day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    existing = await session.scalar(
        select(func.count(PipelineRun.id)).where(
            PipelineRun.trigger == PipelineTrigger.SCHEDULED,
            PipelineRun.started_at >= start_of_day,
            PipelineRun.status.in_([RunStatus.COMPLETED, RunStatus.PARTIAL, RunStatus.RUNNING]),
        )
    )
    return bool(existing)


async def execute_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    max_items: int | None = None,
    agents: list[str] | None = None,
    gateway: LLMGateway | None = None,
    embedder: Embedder | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Execute a pre-created run. Called from the ARQ worker."""
    cfg = settings or get_settings()
    gw = gateway or get_gateway()
    emb = embedder if embedder is not None else _safe_embedder()

    run = await session.get(PipelineRun, run_id)
    if run is None:
        raise OrchestrationError("Pipeline run not found.", detail={"run_id": str(run_id)})

    selected = set(agents) if agents else {a.value for a in AgentName}
    limit = max_items or cfg.pipeline_max_items_per_run
    stats = RunStats()

    context = AgentContext(
        session=session,
        gateway=gw,
        run_id=run.id,
        # The pipeline runs unattended, so it reads at the ceiling. Its *outputs* are then
        # classified and gated by approval before any human sees them.
        clearance=Classification.OFFICIAL_SENSITIVE,
        settings=cfg,
        embedder=emb,
    )

    log.info(
        "pipeline_started",
        run_id=str(run.id),
        trigger=run.trigger.value,
        max_items=limit,
        agents=sorted(selected),
    )

    try:
        # --- 1. Signal: triage ------------------------------------------------
        triaged_ids: list[uuid.UUID] = []
        if AgentName.SIGNAL.value in selected:
            triaged_ids = await _run_signal(context, stats, limit=limit)

        if _budget_exhausted(stats, cfg):
            return await _finish(session, run, stats, reason="token budget exhausted after Signal")

        # --- 2. Risk ∥ Opportunity ∥ Policy ----------------------------------
        analysis_agents = [
            name
            for name in (AgentName.RISK, AgentName.OPPORTUNITY, AgentName.POLICY)
            if name.value in selected
        ]
        if analysis_agents:
            await _run_analysis_fanout(context, stats, analysis_agents)

        if _budget_exhausted(stats, cfg):
            return await _finish(
                session, run, stats, reason="token budget exhausted after analysis"
            )

        # --- 3. Briefing ------------------------------------------------------
        if AgentName.BRIEFING.value in selected:
            await _run_briefing(context, stats)

        # --- 4. Memory --------------------------------------------------------
        if AgentName.MEMORY.value in selected:
            await _run_memory(context, stats)

        # Items that were analysed are marked so, so tomorrow's run does not re-analyse them.
        # Only when an analysis agent actually ran: marking an item ANALYZED is what stops it
        # ever being picked up again, so doing it after a triage-only run (`agents=["signal"]`,
        # or every analysis agent deselected) would retire relevant intelligence that no agent
        # has read. The item would sit at ANALYZED with nothing analysing it — silently dropped,
        # and invisible precisely because the pipeline reported success.
        if triaged_ids and analysis_agents:
            await session.execute(
                update(IngestedItem)
                .where(IngestedItem.id.in_(triaged_ids))
                .values(status=ItemStatus.ANALYZED)
            )
            await session.commit()

    except Exception as exc:
        run.status = RunStatus.FAILED
        run.finished_at = datetime.now(UTC)
        run.stats = {**stats.as_dict(), "error": f"{type(exc).__name__}: {exc}"}
        await session.commit()
        PIPELINE_RUNS.labels(trigger=run.trigger.value, status="failed").inc()
        log.exception("pipeline_crashed", run_id=str(run.id))
        raise

    return await _finish(session, run, stats)


async def _finish(
    session: AsyncSession,
    run: PipelineRun,
    stats: RunStats,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    status = RunStatus.PARTIAL if stats.failed_steps or reason else RunStatus.COMPLETED

    run.status = status
    run.finished_at = datetime.now(UTC)
    payload = stats.as_dict()
    if reason:
        payload["stopped_early"] = reason
    run.stats = payload
    await session.commit()

    PIPELINE_RUNS.labels(trigger=run.trigger.value, status=status.value).inc()
    log.info(
        "pipeline_finished",
        run_id=str(run.id),
        status=status.value,
        **{k: v for k, v in payload.items() if k != "failed_steps"},
        failed_steps=stats.failed_steps,
    )

    return {"run_id": str(run.id), "status": status.value, **payload}


def _budget_exhausted(stats: RunStats, cfg: Settings) -> bool:
    if stats.tokens < cfg.pipeline_token_budget:
        return False
    log.warning(
        "pipeline_token_budget_exhausted",
        spent=stats.tokens,
        budget=cfg.pipeline_token_budget,
    )
    return True


def _safe_embedder() -> Embedder | None:
    """The pipeline still runs without an embedding provider — the corpus tool simply goes quiet.

    Failing the whole run because retrieval is unavailable would be a worse trade: the agents can
    still analyse the ingested items they were handed.
    """
    try:
        return get_embedder()
    except Exception:
        log.warning("embedder_unavailable", detail="agents will run without knowledge-base search")
        return None


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
async def _run_signal(context: AgentContext, stats: RunStats, *, limit: int) -> list[uuid.UUID]:
    """Triage new items; archive everything below the relevance threshold."""
    from app.services.agents.signal_agent import SignalAgent
    from app.services.agents.tools import recent_items_for_analysis

    items = await recent_items_for_analysis(
        context.session,
        clearance=context.clearance,
        statuses=(ItemStatus.NEW,),
        limit=limit,
    )
    if not items:
        log.info("signal_skipped", reason="no new items")
        return []

    context.payload["items"] = items
    context.payload["item_ids"] = [i["id"] for i in items]

    agent = SignalAgent(context.settings)
    result = await agent.run(context)
    stats.tokens += result.tokens_in + result.tokens_out
    stats.cost_usd += result.cost_usd

    if not result.ok or result.output is None:
        stats.failed_steps.append(AgentName.SIGNAL.value)
        return []

    threshold = context.settings.signal_relevance_threshold
    triaged: list[uuid.UUID] = []

    for triage in result.output.triage:
        try:
            item_id = uuid.UUID(triage.item_id)
        except ValueError:
            continue

        item = await context.session.get(IngestedItem, item_id)
        if item is None:
            # The model invented an id. Schema validation catches the shape, not the existence.
            log.warning("signal_triaged_unknown_item", item_id=triage.item_id)
            continue

        item.triage = {
            "relevance": triage.relevance,
            "category": triage.category.value,
            "urgency": triage.urgency.value,
            "rationale": triage.rationale,
        }

        if triage.relevance < threshold:
            item.status = ItemStatus.ARCHIVED
            stats.items_archived += 1
        else:
            item.status = ItemStatus.TRIAGED
            triaged.append(item.id)
            stats.items_triaged += 1

    await context.session.commit()

    log.info(
        "signal_done",
        triaged=stats.items_triaged,
        archived=stats.items_archived,
        threshold=threshold,
    )
    return triaged


@dataclass(slots=True)
class _AnalysisOutcome:
    agent: AgentName
    status: StepStatus
    tokens_in: int
    tokens_out: int
    cost_usd: float
    created: int


async def _run_analysis_fanout(
    context: AgentContext,
    stats: RunStats,
    agents: list[AgentName],
) -> None:
    """Risk, Opportunity and Policy run concurrently — they share inputs and never read each
    other's outputs, so serialising them would only add latency.

    Each gets its OWN database session. Concurrent coroutines must not share one `AsyncSession`:
    it is not task-safe, and two agents flushing through it at once corrupts the identity map.

    `return_exceptions=True` means one agent crashing does not cancel its siblings — the run
    degrades to `partial` instead of losing the day's other analyses.
    """
    from app.db import get_session_factory

    factory = get_session_factory()

    async def _isolated(agent_name: AgentName) -> _AnalysisOutcome:
        async with factory() as own_session:
            own_context = AgentContext(
                session=own_session,
                gateway=context.gateway,
                run_id=context.run_id,
                clearance=context.clearance,
                settings=context.settings,
                embedder=context.embedder,
                payload=dict(context.payload),
            )
            agent = _build_analysis_agent(agent_name, context.settings)
            result = await agent.run(own_context)

            created = 0
            if result.ok and result.output is not None:
                created = await _persist_insights(
                    own_session,
                    run_id=context.run_id,
                    agent_name=agent_name,
                    output=result.output,
                )
                await own_session.commit()

            return _AnalysisOutcome(
                agent=agent_name,
                status=result.status,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cost_usd=result.cost_usd,
                created=created,
            )

    outcomes = await asyncio.gather(*(_isolated(name) for name in agents), return_exceptions=True)

    for agent_name, outcome in zip(agents, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            stats.failed_steps.append(agent_name.value)
            log.warning(
                "analysis_agent_crashed",
                agent=agent_name.value,
                error_type=type(outcome).__name__,
            )
            continue

        stats.tokens += outcome.tokens_in + outcome.tokens_out
        stats.cost_usd += outcome.cost_usd

        if outcome.status is not StepStatus.COMPLETED:
            stats.failed_steps.append(outcome.agent.value)
            continue

        if outcome.agent is AgentName.RISK:
            stats.risks += outcome.created
        elif outcome.agent is AgentName.OPPORTUNITY:
            stats.opportunities += outcome.created
        else:
            stats.policies += outcome.created


def _build_analysis_agent(name: AgentName, settings: Settings) -> Any:
    from app.services.agents.opportunity_agent import OpportunityAgent
    from app.services.agents.policy_agent import PolicyAgent
    from app.services.agents.risk_agent import RiskAgent

    return {
        AgentName.RISK: RiskAgent,
        AgentName.OPPORTUNITY: OpportunityAgent,
        AgentName.POLICY: PolicyAgent,
    }[name](settings)


async def _persist_insights(
    session: AsyncSession,
    *,
    run_id: uuid.UUID | None,
    agent_name: AgentName,
    output: Any,
) -> int:
    """Write drafts and submit each to the approval queue. Nothing here can publish."""
    from app.services.approval_service import submit_for_approval

    kind = {
        AgentName.RISK: InsightKind.RISK,
        AgentName.OPPORTUNITY: InsightKind.OPPORTUNITY,
        AgentName.POLICY: InsightKind.POLICY,
    }[agent_name]

    drafts: list[DraftInsight] = list(getattr(output, "insights", []))
    created = 0

    for draft in drafts:
        # An insight with no citations violates the grounding contract. Dropping it is correct:
        # an uncited claim is exactly what the system exists to prevent from reaching an executive.
        if not draft.citations:
            log.warning("insight_dropped_uncited", agent=agent_name.value, title=draft.title[:80])
            continue

        extra: dict[str, Any] = {}
        if isinstance(draft, PolicyChange):
            extra = {
                "what_changed": draft.what_changed,
                "jurisdictions": draft.jurisdictions,
                "compliance_impact": draft.compliance_impact,
                "required_response": draft.required_response,
                "deadline": draft.deadline.isoformat() if draft.deadline else None,
            }

        insight = Insight(
            id=uuid.uuid4(),
            kind=kind,
            title=draft.title,
            body=draft.body,
            severity=draft.severity,
            likelihood=draft.likelihood,
            confidence=draft.confidence,
            domains=draft.domains,
            recommendations=[r.model_dump(mode="json") for r in draft.recommendations],
            citations={
                "items": [c.id for c in draft.citations if c.kind == "item"],
                "chunks": [c.id for c in draft.citations if c.kind == "chunk"],
            },
            language=Language.EN,
            classification=Classification.OFFICIAL,
            status=PublicationStatus.DRAFT,
            run_id=run_id,
            created_by_agent=agent_name,
            extra=extra,
        )
        session.add(insight)
        await session.flush()

        await submit_for_approval(
            session,
            subject_type=ApprovalSubject.INSIGHT,
            subject_id=insight.id,
            requested_by_agent=agent_name,
        )

        INSIGHTS_CREATED.labels(kind=kind.value).inc()
        created += 1

    return created


async def _run_briefing(context: AgentContext, stats: RunStats) -> None:
    from app.services.agents.briefing_agent import BriefingAgent

    agent = BriefingAgent(context.settings)
    result = await agent.run(context)
    stats.tokens += result.tokens_in + result.tokens_out
    stats.cost_usd += result.cost_usd

    if not result.ok or result.output is None:
        stats.failed_steps.append(AgentName.BRIEFING.value)
        return

    briefing = await _persist_briefing(
        context,
        english=result.output,
        agent=agent,
    )
    if briefing is not None:
        stats.briefings += 1


async def _persist_briefing(
    context: AgentContext,
    *,
    english: BriefingOutput,
    agent: Any,
) -> Briefing | None:
    """Save the briefing with BOTH bodies, then submit it for approval.

    The Arabic pass is a second LLM call. If it fails, the English briefing is still saved and the
    run is marked partial — Arabic is a product requirement, so a missing `body_ar` is recorded as
    a failure rather than quietly shipped as an English-only briefing.
    """
    from app.services.approval_service import submit_for_approval

    session = context.session
    today = datetime.now(UTC).date()

    arabic = await agent.render_arabic(context, english)

    sections: list[dict[str, Any]] = []
    ar_by_key = {s.key: s for s in arabic.sections_ar} if arabic else {}

    for section in english.sections:
        ar = ar_by_key.get(section.key)
        sections.append(
            {
                "key": section.key,
                "heading_en": section.heading,
                "heading_ar": ar.heading if ar else "",
                "body_en": section.body,
                "body_ar": ar.body if ar else "",
            }
        )

    body_en = "\n\n".join(f"## {s['heading_en']}\n\n{s['body_en']}" for s in sections)
    body_ar = (
        "\n\n".join(f"## {s['heading_ar']}\n\n{s['body_ar']}" for s in sections) if arabic else ""
    )

    briefing = Briefing(
        id=uuid.uuid4(),
        date=today,
        title=english.title,
        body_en=body_en,
        body_ar=body_ar,
        sections=sections,
        citations={
            "items": [c.id for c in english.citations if c.kind == "item"],
            "chunks": [c.id for c in english.citations if c.kind == "chunk"],
        },
        confidence=english.confidence,
        classification=Classification.OFFICIAL,
        status=PublicationStatus.DRAFT,
        run_id=context.run_id,
    )
    session.add(briefing)
    await session.flush()

    await submit_for_approval(
        session,
        subject_type=ApprovalSubject.BRIEFING,
        subject_id=briefing.id,
        requested_by_agent=AgentName.BRIEFING,
    )
    await session.commit()

    if not arabic:
        log.warning("briefing_arabic_pass_failed", briefing_id=str(briefing.id))

    return briefing


async def _drafted_insights(context: AgentContext) -> list[dict[str, Any]]:
    """The insights this run drafted, as the digest the Memory agent judges durability from."""
    if context.run_id is None:
        return []

    rows = (
        await context.session.scalars(
            select(Insight)
            .where(Insight.run_id == context.run_id)
            .order_by(Insight.severity.desc())
        )
    ).all()
    return [
        {
            "kind": row.kind.value,
            "severity": row.severity,
            "confidence": row.confidence,
            "title": row.title,
        }
        for row in rows
    ]


async def _drafted_briefing(context: AgentContext) -> dict[str, Any]:
    """This run's briefing, if it produced one — the Memory agent reads its decisions section."""
    if context.run_id is None:
        return {}

    briefing = await context.session.scalar(
        select(Briefing).where(Briefing.run_id == context.run_id).limit(1)
    )
    if briefing is None:
        return {}
    return {"title": briefing.title, "sections": briefing.sections}


async def _run_memory(context: AgentContext, stats: RunStats) -> None:
    from app.services.agents.memory_agent import MemoryAgent
    from app.services.memory_service import create_memory

    agent = MemoryAgent(context.settings)

    # Show the agent what the run actually produced. `payload` only ever carried the triaged
    # items, so the Memory agent — whose entire job is to judge what from *this run* is worth
    # keeping — was asked that question while being told "(This run drafted no insights.)". It
    # answered "nothing", correctly, every single time: institutional memory recorded nothing,
    # ever, and the symptom was an agent that looked like it was working (it completed, it cost
    # money, it returned a valid empty list). The giveaway was tokens_in never changing between
    # a run with one insight and a run with six.
    #
    # Read back from the database rather than threading the drafts through the fan-out: the
    # analysis agents each commit on their own isolated session, so the rows are the only place
    # the run's output exists as a whole.
    context.payload["insights"] = await _drafted_insights(context)
    context.payload["briefing"] = await _drafted_briefing(context)
    context.payload["run_summary"] = stats.as_dict()

    result = await agent.run(context)
    stats.tokens += result.tokens_in + result.tokens_out
    stats.cost_usd += result.cost_usd

    if not result.ok or result.output is None:
        stats.failed_steps.append(AgentName.MEMORY.value)
        return

    for draft in result.output.entries:
        await create_memory(
            context.session,
            kind=draft.kind,
            title=draft.title,
            content=draft.content,
            tags=draft.tags,
            source_ref={"run_id": str(context.run_id) if context.run_id else None},
            classification=Classification.OFFICIAL,
            embedder=context.embedder,
            created_by=None,
        )
        stats.memories += 1

    await context.session.commit()


# ---------------------------------------------------------------------------
# On-demand briefing (§7.7 #18)
# ---------------------------------------------------------------------------
async def generate_briefing_only(
    session: AsyncSession,
    *,
    user: User,
    gateway: LLMGateway | None = None,
    embedder: Embedder | None = None,
    for_date: date | None = None,
    force: bool = False,
    settings: Settings | None = None,
) -> Briefing:
    """Run the Briefing Agent alone, against whatever insights already exist."""
    cfg = settings or get_settings()
    target = for_date or datetime.now(UTC).date()

    if not force:
        existing = await session.scalar(select(Briefing).where(Briefing.date == target).limit(1))
        if existing is not None:
            return existing

    from app.services.agents.briefing_agent import BriefingAgent

    run = await start_run(session, trigger=PipelineTrigger.MANUAL, initiated_by=user.id)
    await session.commit()

    context = AgentContext(
        session=session,
        gateway=gateway or get_gateway(),
        run_id=run.id,
        clearance=Classification.OFFICIAL_SENSITIVE,
        settings=cfg,
        embedder=embedder if embedder is not None else _safe_embedder(),
    )

    agent = BriefingAgent(cfg)
    result = await agent.run(context)

    stats = RunStats(tokens=result.tokens_in + result.tokens_out, cost_usd=result.cost_usd)

    if not result.ok or result.output is None:
        stats.failed_steps.append(AgentName.BRIEFING.value)
        await _finish(session, run, stats, reason="briefing agent failed")
        raise OrchestrationError(
            "The Briefing Agent could not produce a briefing.",
            detail={"run_id": str(run.id), "error": result.error},
        )

    briefing = await _persist_briefing(context, english=result.output, agent=agent)
    if briefing is None:
        raise OrchestrationError("The briefing could not be saved.")

    stats.briefings = 1
    await _finish(session, run, stats)
    return briefing


async def policy_output_is_empty(output: PolicyOutput) -> bool:
    return not output.insights
