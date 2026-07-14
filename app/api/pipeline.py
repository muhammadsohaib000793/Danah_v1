"""Pipeline API (§7.7 #13–14): trigger a run, poll its live per-step ledger.

Mounted at /api/pipeline.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import Subquery, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.deps import client_ip, get_config, get_current_user, get_db, require_analyst
from app.enums import (
    ActorType,
    Classification,
    PipelineTrigger,
    PublicationStatus,
    RunStatus,
    classification_at_or_below,
)
from app.exceptions import NotFoundError
from app.models import Briefing, Insight, PipelineRun, PipelineStep, User
from app.schemas.common import Page
from app.schemas.pipeline import (
    PipelineRunAccepted,
    PipelineRunDetail,
    PipelineRunOut,
    PipelineRunRequest,
    PipelineStepOut,
)
from app.security.rbac import user_clearance
from app.services.audit_service import record_audit

log = structlog.get_logger(__name__)

router = APIRouter(tags=["pipeline"])


def _rollups() -> Subquery:
    """Per-run token/cost/step totals, so the list endpoint does not fan out one query per run."""
    return (
        select(
            PipelineStep.run_id.label("run_id"),
            func.coalesce(func.sum(PipelineStep.tokens_in + PipelineStep.tokens_out), 0).label(
                "total_tokens"
            ),
            func.coalesce(func.sum(PipelineStep.cost_usd), 0).label("total_cost"),
            func.count(PipelineStep.id).label("step_count"),
        )
        .group_by(PipelineStep.run_id)
        .subquery()
    )


def _duration_ms(started_at: datetime, finished_at: datetime | None) -> int | None:
    if finished_at is None:
        return None
    return int((finished_at - started_at).total_seconds() * 1000)


def run_out(
    run: PipelineRun,
    *,
    total_tokens: int,
    total_cost_usd: float,
    step_count: int,
) -> PipelineRunOut:
    return PipelineRunOut(
        id=run.id,
        trigger=run.trigger,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        stats=run.stats,
        initiated_by=run.initiated_by,
        total_tokens=total_tokens,
        total_cost_usd=total_cost_usd,
        duration_ms=_duration_ms(run.started_at, run.finished_at),
        step_count=step_count,
    )


async def latest_run(db: AsyncSession) -> PipelineRunOut | None:
    """The most recent run with its roll-ups — the dashboard's `latest_run` panel."""
    rollups = _rollups()
    row = (
        await db.execute(
            select(
                PipelineRun,
                func.coalesce(rollups.c.total_tokens, 0),
                func.coalesce(rollups.c.total_cost, 0),
                func.coalesce(rollups.c.step_count, 0),
            )
            .outerjoin(rollups, rollups.c.run_id == PipelineRun.id)
            .order_by(PipelineRun.started_at.desc())
            .limit(1)
        )
    ).one_or_none()

    if row is None:
        return None

    run, tokens, cost, steps = row
    return run_out(
        run,
        total_tokens=int(tokens),
        total_cost_usd=float(cost),
        step_count=int(steps),
    )


async def _enqueue_run(
    run_id: uuid.UUID,
    settings: Settings,
    *,
    max_items: int | None,
    agents: list[str] | None,
) -> None:
    """Hand the run to the ARQ worker.

    A Redis outage must not lose the run: the `pipeline_runs` row is already committed, so the
    caller still holds a `run_id` it can poll and an operator can re-drive the job by hand
    (docs/RUNBOOK.md). Failing the request here would tell the caller that nothing happened, when
    a run row exists and will show as `running` forever if it is not re-driven.
    """
    from arq import create_pool
    from arq.connections import RedisSettings

    from app.logging import get_request_id

    try:
        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        # `request_id` is an ordinary task argument. ARQ has no job-metadata channel: it reserves
        # only `_job_id`, `_queue_name`, `_defer_until`, `_defer_by`, `_expires` and `_job_try`,
        # and forwards everything else to the task. `_request_id=` was therefore handed to
        # `run_pipeline` as an unexpected keyword and every manual run died on a TypeError while
        # the API still returned 202 and a pollable id.
        await pool.enqueue_job(
            "run_pipeline",
            str(run_id),
            PipelineTrigger.MANUAL.value,
            max_items,
            agents,
            request_id=get_request_id(),
        )
        await pool.aclose()
    except Exception as exc:
        log.error(
            "enqueue_run_pipeline_failed",
            run_id=str(run_id),
            error=str(exc),
            hint="The run row exists and can be re-driven; it stays 'running' until it is.",
        )


@router.post(
    "/run",
    response_model=PipelineRunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger an agent pipeline run (analyst+)",
    description=(
        "Creates the run **synchronously** and returns its id immediately, then executes "
        "Signal → (Risk ∥ Opportunity ∥ Policy) → Briefing → Memory in the background. Poll "
        "`GET /api/pipeline/runs/{run_id}` for live per-step status, tokens and cost.\n\n"
        "Nothing the run produces is published: every insight and briefing lands in the approval "
        "queue."
    ),
)
async def trigger_run(
    payload: PipelineRunRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_analyst),
    settings: Settings = Depends(get_config),
) -> PipelineRunAccepted:
    # Imported inside the handler: the orchestrator pulls in all six agents and the provider SDKs
    # behind them, and only these two routes need that graph.
    from app.services.orchestrator import start_run

    run = await start_run(
        db,
        trigger=PipelineTrigger.MANUAL,
        initiated_by=user.id,
        max_items=payload.max_items,
    )
    agents = [a.value for a in payload.agents] if payload.agents else None

    await record_audit(
        db,
        action="pipeline.run",
        actor_type=ActorType.USER,
        actor_id=user.id,
        subject_type="pipeline_run",
        subject_id=run.id,
        ip=client_ip(request),
        detail={
            "trigger": run.trigger.value,
            "max_items": payload.max_items,
            "agents": agents,
        },
    )

    # Committed *before* the job is enqueued: the worker runs in another process and would not
    # find the run row if the queue beat this transaction to it.
    await db.commit()

    await _enqueue_run(run.id, settings, max_items=payload.max_items, agents=agents)

    log.info(
        "pipeline_run_accepted",
        run_id=str(run.id),
        initiated_by=str(user.id),
        max_items=payload.max_items,
    )
    return PipelineRunAccepted(run_id=run.id, status=run.status)


@router.get(
    "/runs",
    response_model=Page[PipelineRunOut],
    summary="List pipeline runs, newest first",
)
async def list_runs(
    run_status: RunStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Page[PipelineRunOut]:
    rollups = _rollups()

    stmt = (
        select(
            PipelineRun,
            func.coalesce(rollups.c.total_tokens, 0),
            func.coalesce(rollups.c.total_cost, 0),
            func.coalesce(rollups.c.step_count, 0),
        )
        .outerjoin(rollups, rollups.c.run_id == PipelineRun.id)
        .order_by(PipelineRun.started_at.desc())
        .limit(limit)
        .offset(offset)
    )
    count_stmt = select(func.count(PipelineRun.id))

    if run_status is not None:
        stmt = stmt.where(PipelineRun.status == run_status)
        count_stmt = count_stmt.where(PipelineRun.status == run_status)

    total = await db.scalar(count_stmt)
    rows = (await db.execute(stmt)).all()

    return Page[PipelineRunOut](
        items=[
            run_out(
                run,
                total_tokens=int(tokens),
                total_cost_usd=float(cost),
                step_count=int(steps),
            )
            for run, tokens, cost, steps in rows
        ],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/runs/{run_id}",
    response_model=PipelineRunDetail,
    summary="One run: live per-step status, tokens, cost and latency",
    description=(
        "The view the UI polls while a run is in flight. A step appears as `running` the moment "
        "the agent starts, and carries its own token and cost ledger when it finishes."
    ),
)
async def get_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PipelineRunDetail:
    run = (
        await db.scalars(
            select(PipelineRun)
            .where(PipelineRun.id == run_id)
            .options(selectinload(PipelineRun.steps))
        )
    ).one_or_none()

    if run is None:
        raise NotFoundError("No such pipeline run.", detail={"run_id": str(run_id)})

    steps = [
        PipelineStepOut(
            id=step.id,
            agent=step.agent,
            status=step.status,
            tokens_in=step.tokens_in,
            tokens_out=step.tokens_out,
            cost_usd=float(step.cost_usd),
            latency_ms=step.latency_ms,
            error=step.error,
            input_ref=step.input_ref,
            output_ref=step.output_ref,
            created_at=step.created_at,
        )
        for step in run.steps
    ]

    clearance: Classification = user_clearance(user)
    insight_count = await _run_insight_count(db, run_id=run.id, clearance=clearance)
    briefing_count = await _run_briefing_count(db, run_id=run.id, clearance=clearance)

    base = run_out(
        run,
        total_tokens=sum(s.tokens_in + s.tokens_out for s in steps),
        total_cost_usd=float(run.total_cost_usd),
        step_count=len(steps),
    )
    return PipelineRunDetail(
        **base.model_dump(),
        steps=steps,
        insight_count=insight_count,
        briefing_count=briefing_count,
    )


async def _run_insight_count(
    db: AsyncSession, *, run_id: uuid.UUID, clearance: Classification
) -> int:
    """Counts only what this caller could open — a count is a disclosure like any other."""
    total = await db.scalar(
        select(func.count(Insight.id)).where(
            Insight.run_id == run_id,
            Insight.classification.in_(classification_at_or_below(clearance)),
            Insight.status != PublicationStatus.REJECTED,
        )
    )
    return int(total or 0)


async def _run_briefing_count(
    db: AsyncSession, *, run_id: uuid.UUID, clearance: Classification
) -> int:
    total = await db.scalar(
        select(func.count(Briefing.id)).where(
            Briefing.run_id == run_id,
            Briefing.classification.in_(classification_at_or_below(clearance)),
            Briefing.status != PublicationStatus.REJECTED,
        )
    )
    return int(total or 0)
