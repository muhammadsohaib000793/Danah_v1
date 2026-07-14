"""Background tasks.

Each task owns its own DB session (a worker has no FastAPI request scope) and re-establishes the
request id, so a job's logs correlate with the API call that enqueued it.

Tasks are thin: resolve arguments, call the service that does the real work, record the outcome.
The business logic lives in `app/services/`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import text

from app.db import get_session_factory
from app.enums import PipelineTrigger
from app.logging import new_request_id, set_request_id

log = structlog.get_logger(__name__)


def bind_request_id(ctx: dict[str, Any], request_id: str | None = None) -> str:
    """Carry the enqueuing request's id into the job, or mint one for cron-originated work.

    The id has to be passed as an ordinary task argument. ARQ has no job-metadata channel and
    never populates `ctx["request_id"]`: its `enqueue_job` reserves only `_job_id`, `_queue_name`,
    `_defer_until`, `_defer_by`, `_expires` and `_job_try`, and forwards every other keyword to
    the task itself. Enqueuing with `_request_id=...` therefore did not annotate the job — it
    called `embed_document(ctx, doc_id, _request_id=...)`, which raises TypeError before the task
    body runs. Uploads sat at `pending` and manual pipeline runs never started, while the API
    happily returned 202.
    """
    resolved = request_id or ctx.get("request_id") or new_request_id()
    set_request_id(str(resolved))
    return str(resolved)


async def worker_ping(ctx: dict[str, Any]) -> dict[str, Any]:
    """End-to-end proof that the queue is being consumed and the worker can reach Postgres.

    Enqueue it and read back the result to distinguish "the worker is down" from "the job is
    stuck" — see `docs/RUNBOOK.md`. This is the only task with no service behind it, by design.
    """
    bind_request_id(ctx)
    factory = get_session_factory()
    async with factory() as session:
        db_ok = bool(await session.scalar(text("SELECT 1")))

    result = {"ok": db_ok, "at": datetime.now(UTC).isoformat(), "job_id": str(ctx.get("job_id"))}
    log.info("worker_ping", **result)
    return result


# --- Phase 1 ---------------------------------------------------------------
async def embed_document(
    ctx: dict[str, Any], document_id: str, request_id: str | None = None
) -> dict[str, Any]:
    """Extract → chunk → embed → index one uploaded document.

    `index_document` records its own failure on the row (status `failed`, reason in `error`), so
    this task does not re-raise: an ARQ retry would re-run an extraction that is deterministically
    going to fail again, and the user already has the reason in the API.
    """
    bind_request_id(ctx, request_id)
    from app.services.rag.indexer import index_document

    factory = get_session_factory()
    async with factory() as session:
        result = await index_document(session, uuid.UUID(document_id))
        await session.commit()

    log.info(
        "task_embed_document_done",
        document_id=document_id,
        chunks=result.chunk_count,
        status=result.status.value,
    )
    return {
        "document_id": document_id,
        "chunks": result.chunk_count,
        "status": result.status.value,
        "error": result.error,
    }


# --- Phase 2 ---------------------------------------------------------------
async def sync_source(ctx: dict[str, Any], source_id: str) -> dict[str, Any]:
    """Fetch one source and persist its new items (deduplicated)."""
    bind_request_id(ctx)
    from app.services.ingestion.runner import sync_source_by_id

    factory = get_session_factory()
    async with factory() as session:
        result = await sync_source_by_id(session, uuid.UUID(source_id))
        await session.commit()

    log.info(
        "task_sync_source_done",
        source_id=source_id,
        fetched=result.fetched,
        created=result.created,
        duplicates=result.duplicates,
        status=result.status,
    )
    return result.model_dump(mode="json")


async def sync_all_due_sources(ctx: dict[str, Any]) -> dict[str, Any]:
    """Cron tick: enqueue a sync for every enabled source whose poll interval has elapsed.

    The tick enqueues rather than executes, so one slow source cannot delay the others and a long
    sync cannot overrun the next tick.
    """
    bind_request_id(ctx)
    from app.services.ingestion.runner import due_source_ids

    factory = get_session_factory()
    async with factory() as session:
        due = await due_source_ids(session)

    redis = ctx.get("redis")
    enqueued = 0
    for source_id in due:
        if redis is not None:
            await redis.enqueue_job("sync_source", str(source_id))
            enqueued += 1

    log.info("task_sync_all_due_sources", due=len(due), enqueued=enqueued)
    return {"due": len(due), "enqueued": enqueued}


async def run_pipeline(
    ctx: dict[str, Any],
    run_id: str,
    trigger: str = PipelineTrigger.MANUAL.value,
    max_items: int | None = None,
    agents: list[str] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Execute a full agent pipeline run against a pre-created `pipeline_runs` row.

    The row is created synchronously by `POST /api/pipeline/run` so the caller gets a `run_id` to
    poll immediately; this task fills it in.
    """
    bind_request_id(ctx, request_id)
    from app.services.orchestrator import execute_run

    factory = get_session_factory()
    async with factory() as session:
        summary = await execute_run(
            session,
            run_id=uuid.UUID(run_id),
            max_items=max_items,
            agents=agents,
        )
        await session.commit()

    log.info("task_run_pipeline_done", run_id=run_id, status=summary.get("status"))
    return summary


# --- Phase 3 ---------------------------------------------------------------
async def daily_brief(ctx: dict[str, Any]) -> dict[str, Any]:
    """Cron: the scheduled daily pipeline run, ending in a bilingual executive briefing.

    Idempotent per day (§7.4): if a scheduled run already happened today, this is a no-op rather
    than a second run and a duplicate briefing.
    """
    bind_request_id(ctx)
    from app.services.orchestrator import start_run, was_scheduled_run_completed_today

    factory = get_session_factory()
    async with factory() as session:
        if await was_scheduled_run_completed_today(session):
            log.info("task_daily_brief_skipped", reason="already_ran_today")
            return {"skipped": True, "reason": "already_ran_today"}

        run = await start_run(session, trigger=PipelineTrigger.SCHEDULED, initiated_by=None)
        run_id = run.id
        await session.commit()

    redis = ctx.get("redis")
    if redis is not None:
        await redis.enqueue_job("run_pipeline", str(run_id), PipelineTrigger.SCHEDULED.value)

    log.info("task_daily_brief_enqueued", run_id=str(run_id))
    return {"skipped": False, "run_id": str(run_id)}


async def check_daily_cost(ctx: dict[str, Any]) -> dict[str, Any]:
    """Cron: notify administrators when today's LLM spend exceeds DAILY_COST_ALERT_USD."""
    bind_request_id(ctx)
    from app.config import get_settings
    from app.services.llm.usage_tracker import today_cost_usd
    from app.services.notification_service import notify_cost_alert

    settings = ctx.get("settings") or get_settings()
    threshold = float(settings.daily_cost_alert_usd)

    factory = get_session_factory()
    async with factory() as session:
        spent = float(await today_cost_usd(session))
        exceeded = spent > threshold
        if exceeded:
            await notify_cost_alert(session, spent_usd=spent, threshold_usd=threshold)
            await session.commit()

    log.info("task_check_daily_cost", spent_usd=spent, threshold_usd=threshold, exceeded=exceeded)
    return {
        "spent_usd": spent,
        "threshold_usd": threshold,
        "exceeded": exceeded,
        "date": datetime.now(UTC).date().isoformat(),
    }
