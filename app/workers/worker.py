"""ARQ worker and cron scheduler.

Two entrypoints share one task registry:

  * `WorkerSettings`    — `arq app.workers.worker.WorkerSettings`
    Consumes the queue: source syncs, document embedding, pipeline runs.

  * `SchedulerSettings` — `arq app.workers.worker.SchedulerSettings`
    Runs cron only, and *enqueues* rather than executes, so a slow pipeline can never
    block the next tick. Separating them means scaling workers does not multiply cron firings.
"""

from __future__ import annotations

from typing import Any

import structlog
from arq import cron
from arq.connections import RedisSettings

from app.config import get_settings
from app.db import dispose_engine
from app.logging import configure_logging, new_request_id, set_request_id
from app.workers import tasks

log = structlog.get_logger(__name__)


def build_redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings)
    set_request_id(new_request_id())
    ctx["settings"] = settings
    log.info("worker_startup", redis=settings.redis_url.split("@")[-1])


async def shutdown(ctx: dict[str, Any]) -> None:
    await dispose_engine()
    log.info("worker_shutdown")


FUNCTIONS: list[Any] = [
    tasks.worker_ping,
    tasks.embed_document,
    tasks.sync_source,
    tasks.sync_all_due_sources,
    tasks.run_pipeline,
    tasks.daily_brief,
    tasks.check_daily_cost,
]


class WorkerSettings:
    """Queue consumer.

    NB: ARQ reads `redis_settings` as a plain class attribute holding a `RedisSettings`
    instance — a method or staticmethod here fails at pool creation with
    `'staticmethod' object has no attribute 'host'`.
    """

    functions = FUNCTIONS
    redis_settings = build_redis_settings()
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 10
    job_timeout = 900  # a full pipeline run over 150 items can legitimately take minutes
    keep_result = 3600
    max_tries = 3
    health_check_interval = 60


def _cron_jobs() -> list[Any]:
    """Cron schedule, derived from config.

    `PIPELINE_SCHEDULE_CRON` (default `0 5 * * *`) drives the daily run. ARQ's `cron()` takes
    explicit fields rather than a crontab string, so the expression is parsed here.

    Every cron job *enqueues* rather than executes. A twelve-minute pipeline run must not block
    the source-polling tick behind it.
    """
    settings = get_settings()
    minute, hour = _parse_cron_hh_mm(settings.pipeline_schedule_cron)

    # `name=` is load-bearing, not cosmetic. arq defaults a cron job's name to
    # `cron:<function>`, enqueues it onto the shared queue under *that* name, and only registers
    # it in the registry of a worker that was given `cron_jobs`. The queue-consuming worker has
    # none — so it dequeued every scheduled job and failed it with "function not found", while
    # the scheduler that could run them never got the chance. Silent: cron fired on time, the
    # logs said a job was picked up, and no source was ever polled. Naming each cron job after
    # the task the worker already registers is what makes "the scheduler enqueues, the worker
    # executes" true rather than merely intended.
    return [
        # Check every 5 minutes for sources that are due. The task itself honours each source's
        # `poll_interval_minutes`, so this tick is cheap and idempotent.
        cron(
            tasks.sync_all_due_sources,
            name="sync_all_due_sources",
            minute=set(range(0, 60, 5)),
            run_at_startup=False,
        ),
        # The daily agent pipeline, ending in the bilingual executive briefing.
        cron(
            tasks.daily_brief,
            name="daily_brief",
            hour={hour},
            minute={minute},
            run_at_startup=False,
        ),
        # Cost guardrail: notify administrators if DAILY_COST_ALERT_USD was exceeded today.
        cron(
            tasks.check_daily_cost,
            name="check_daily_cost",
            hour={23},
            minute={55},
            run_at_startup=False,
        ),
    ]


def _parse_cron_hh_mm(expression: str) -> tuple[int, int]:
    """Extract (minute, hour) from a 5-field crontab expression.

    Only the minute and hour fields are used — the daily pipeline runs every day, so the
    day/month/weekday fields are not meaningful here. A malformed expression falls back to
    05:00 rather than preventing the scheduler from starting.
    """
    parts = expression.split()
    try:
        minute = int(parts[0])
        hour = int(parts[1])
    except (IndexError, ValueError):
        log.warning("invalid_pipeline_schedule_cron", expression=expression, fallback="0 5 * * *")
        return 0, 5
    if not (0 <= minute <= 59 and 0 <= hour <= 23):
        log.warning("out_of_range_pipeline_schedule_cron", expression=expression)
        return 0, 5
    return minute, hour


class SchedulerSettings:
    """Cron only. Enqueues work; the worker executes it."""

    functions = FUNCTIONS
    cron_jobs = _cron_jobs()
    redis_settings = build_redis_settings()
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 4
