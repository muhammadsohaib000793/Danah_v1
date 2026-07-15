"""FastAPI application factory.

Middleware order matters and is deliberate (outermost first):
  1. RequestContextMiddleware — assigns the request id everything else logs against.
  2. CORSMiddleware          — must see the response of every inner layer, including errors.
  3. RateLimitMiddleware     — rejects before any handler work (Phase 4).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import health
from app.config import Settings, get_settings
from app.db import dispose_engine
from app.exceptions import register_exception_handlers
from app.logging import RequestContextMiddleware, configure_logging
from app.security.rate_limit import RateLimitMiddleware

log = structlog.get_logger(__name__)

DESCRIPTION = """
**DANAH** — Strategic Intelligence Platform.

Continuous ingestion of external signals → a pipeline of six specialised AI agents →
grounded, cited, human-approved intelligence, in English and Arabic.

**Every AI output is grounded or silent:** answers carry citations and a confidence score, or
explicitly abstain. **Nothing an agent produces is published without a human approval decision.**
Classification (`PUBLIC` → `OFFICIAL_SENSITIVE`) is enforced in SQL at the data layer, never
client-side.
"""

TAGS_METADATA = [
    {"name": "auth", "description": "Login, token refresh, current user."},
    {"name": "chat", "description": "Grounded chat over the document corpus, with citations."},
    {"name": "knowledge", "description": "Document upload, indexing status, semantic search."},
    {"name": "sources", "description": "External data sources and their health."},
    {"name": "items", "description": "Ingested signal items and their Signal-Agent triage."},
    {"name": "pipeline", "description": "Agent pipeline runs with live per-step token and cost."},
    {"name": "insights", "description": "Risk, opportunity and policy insights."},
    {"name": "briefings", "description": "Bilingual (EN/AR) executive briefings."},
    {"name": "approvals", "description": "The human-in-the-loop publication gate."},
    {"name": "dashboard", "description": "Single-call summary powering the command centre."},
    {"name": "memory", "description": "Institutional memory: decisions, lessons, context."},
    {"name": "notifications", "description": "In-app notifications."},
    {"name": "audit", "description": "Hash-chained audit trail and chain verification."},
    {"name": "admin", "description": "User and system administration."},
    {"name": "ingest", "description": "Webhook ingestion for licensed feeds (HMAC-secured)."},
    {"name": "ops", "description": "Health and metrics."},
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    log.info(
        "startup",
        app=settings.app_name,
        version=__version__,
        environment=settings.app_env.value,
        llm_provider=settings.llm_provider.value,
        llm_configured=settings.has_llm_credentials,
        embeddings_configured=settings.has_embedding_credentials,
    )
    if not settings.has_llm_credentials:
        log.warning(
            "llm_not_configured",
            detail=(
                "No provider key set. The API runs, but chat/agent routes return 503 "
                "instead of fabricating answers. See FIRST_RUN.md."
            ),
        )
    yield
    await dispose_engine()
    log.info("shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or get_settings()
    configure_logging(cfg)

    app = FastAPI(
        title=f"{cfg.app_name} — Strategic Intelligence Platform",
        description=DESCRIPTION,
        version=__version__,
        openapi_tags=TAGS_METADATA,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # --- Middleware -----------------------------------------------------------
    # Starlette applies middleware in reverse registration order, so the LAST one added is the
    # OUTERMOST. The intended order, outermost first:
    #   RequestContext  -> assigns the request id everything else logs and audits against
    #   CORS            -> must see every inner response, including error responses
    #   RateLimit       -> rejects before any handler or database work happens
    app.add_middleware(RateLimitMiddleware, settings=cfg)
    app.add_middleware(
        CORSMiddleware,
        # `null` is present for local file:// testing of the v11 HTML prototype.
        # ⚠️ REMOVE IT IN PRODUCTION — config.py refuses to start if APP_ENV=production
        # and `null` is still in CORS_ORIGINS.
        allow_origins=cfg.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[cfg.request_id_header, "Retry-After"],
    )
    app.add_middleware(RequestContextMiddleware, header_name=cfg.request_id_header)

    register_exception_handlers(app)

    # --- Routers ------------------------------------------------------------
    app.include_router(health.router, prefix="/api")

    _register_optional_routers(app)

    if cfg.metrics_enabled:
        _install_metrics(app)

    _mount_ui(app)

    return app


def _mount_ui(app: FastAPI) -> None:
    """Serve the command centre from the API itself, at `/`.

    Same origin as the API on purpose. The alternative — a separate static host — buys
    nothing here and costs a CORS surface: the browser would need cross-origin credentialed
    requests, which means relaxing `CORS_ORIGINS` on a system holding OFFICIAL-SENSITIVE
    data. Served from `/`, the UI calls `/api/...` as a same-origin request and no
    cross-origin exception is needed at all.

    Mounted last, so it can never shadow an API route.
    """
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    web = Path(__file__).resolve().parent.parent / "web"
    if not (web / "index.html").exists():
        log.info("ui_not_mounted", reason="web/index.html is absent", path=str(web))
        return

    app.mount("/", StaticFiles(directory=str(web), html=True), name="ui")
    log.info("ui_mounted", path=str(web), url="/")


def _register_optional_routers(app: FastAPI) -> None:
    """Mount every feature router that exists.

    Routers land phase by phase; this keeps `main.py` from being edited on every step and
    lets Phase 0 boot with only /healthz.
    """
    from importlib import import_module

    modules = [
        ("app.api.auth", "/api/auth"),
        ("app.api.chat", "/api/agent"),
        ("app.api.knowledge", "/api/knowledge"),
        ("app.api.sources", "/api/sources"),
        ("app.api.items", "/api/items"),
        ("app.api.tasks", "/api/tasks"),
        ("app.api.pipeline", "/api/pipeline"),
        ("app.api.insights", "/api/insights"),
        ("app.api.briefings", "/api/briefings"),
        ("app.api.approvals", "/api/approvals"),
        ("app.api.dashboard", "/api/dashboard"),
        ("app.api.memory", "/api/memory"),
        ("app.api.notifications", "/api/notifications"),
        ("app.api.audit", "/api/audit"),
        ("app.api.admin", "/api/admin"),
        ("app.api.ingest", "/api/ingest"),
    ]
    for module_path, prefix in modules:
        try:
            module = import_module(module_path)
        except ModuleNotFoundError:
            continue
        router = getattr(module, "router", None)
        if router is not None:
            app.include_router(router, prefix=prefix)


def _install_metrics(app: FastAPI) -> None:
    """Prometheus `/metrics`: request latency/errors plus DANAH's own LLM cost counters."""
    from app.metrics import MetricsMiddleware, metrics_endpoint

    app.add_middleware(MetricsMiddleware)
    app.add_route("/metrics", metrics_endpoint, methods=["GET"], include_in_schema=True)


app = create_app()
