"""Test harness.

Two decisions that matter here:

1. **The test database is a real PostgreSQL + pgvector database** (`danah_test`), not SQLite.
   The schema depends on pgvector, Postgres FTS, `jsonb`, `text[]`, `bigserial` and an
   append-only trigger — none of which SQLite has. Testing against SQLite would test a
   *different* schema than the one that ships (docs/DECISIONS.md #14).

2. **No test ever calls a real LLM** (master prompt §12). `FakeLLMGateway` and `FakeEmbedder`
   substitute at the *gateway interface*, so everything below them — agents, orchestrator,
   retriever, composer, API layer — is the real production code path.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# --- Environment must be set BEFORE app.config is imported anywhere ---------
_TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://danah:danah@localhost:5433/danah_test",
)
os.environ["DATABASE_URL"] = _TEST_DB_URL
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("APP_DEBUG", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-not-used-in-production-0123456789abcdef")
os.environ.setdefault("ADMIN_EMAIL", "admin@ministry.gov")
os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "test-admin-password-123")
os.environ.setdefault("WEBHOOK_HMAC_DEFAULT_SECRET", "test-webhook-hmac-secret")
# Providers are deliberately unset: any code path that tries to reach a real provider in a
# test will fail loudly rather than silently hitting the network.
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""
os.environ["VOYAGE_API_KEY"] = ""

from app.config import Settings, get_settings
from app.db import Base
from app.enums import Classification, Role

# Derived, never pinned: the fake embedder must emit whatever width the configured provider
# emits, or every vector write fails against the `vector(n)` column the migration built from
# the same setting. Hard-coding this made the whole integration suite fail on the Voyage→OpenAI
# switch (1024 → 1536) for a reason that had nothing to do with the code under test.
EMBEDDING_DIM = get_settings().embedding_dim


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
# NB: no custom `event_loop` fixture — pytest-asyncio 1.x removed that hook. The single
# session-scoped loop is configured in pyproject.toml (asyncio_default_*_loop_scope).
@pytest.fixture(scope="session")
def settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


# ---------------------------------------------------------------------------
# Database — schema created once per session, data truncated between tests
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture(scope="session")
async def engine() -> AsyncIterator[Any]:
    eng = create_async_engine(_TEST_DB_URL, echo=False, pool_pre_ping=True)

    async with eng.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))

    # Build the schema by running the real migration, so tests exercise exactly the DDL that
    # ships (HNSW indexes, generated tsvector columns, the append-only audit trigger).
    await _run_migrations(eng)

    yield eng

    await eng.dispose()


async def _run_migrations(eng: Any) -> None:
    from alembic import command
    from alembic.config import Config

    def _upgrade() -> None:
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", _TEST_DB_URL)
        # Drop everything first so a re-run starts from a known-clean schema.
        command.upgrade(cfg, "head")

    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: _drop_everything(sync_conn))

    await asyncio.to_thread(_upgrade)


def _drop_everything(sync_conn: Any) -> None:
    sync_conn.exec_driver_sql("DROP SCHEMA public CASCADE")
    sync_conn.exec_driver_sql("CREATE SCHEMA public")
    sync_conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
    sync_conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pgcrypto")


@pytest_asyncio.fixture
async def db(engine: Any) -> AsyncIterator[AsyncSession]:
    """A session per test. Every table is truncated afterwards.

    `audit_log` needs its append-only trigger disabled to be truncated — which is exactly the
    privileged path the Phase-4 tamper test uses, and proves the guard is real.
    """
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()

    tables = [t.name for t in reversed(Base.metadata.sorted_tables)]
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_truncate"))
        await conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
        await conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_truncate"))


@pytest_asyncio.fixture(autouse=True)
async def _reset_rate_limit_window() -> AsyncIterator[None]:
    """Give every test an empty rate-limit window.

    The limiter's window lives in Redis, not in Postgres, so the `db` fixture's TRUNCATE does
    not touch it. Without this, requests from one test count against the next: the suite shares
    a single client identity, so a busy test silently spends the budget of the tests that follow
    and they start failing with 429s that have nothing to do with what they assert.
    """
    from app.security.rate_limit import reset_limiter

    async def _flush() -> None:
        try:
            client = Redis.from_url(get_settings().redis_url, socket_connect_timeout=1)
            keys = [k async for k in client.scan_iter("ratelimit:*")]
            if keys:
                await client.delete(*keys)
            await client.aclose()
        except (RedisError, OSError):
            pass  # No Redis: the limiter fails open anyway, so there is nothing to clear.

    reset_limiter()
    await _flush()
    yield
    reset_limiter()
    await _flush()


# ---------------------------------------------------------------------------
# Fake providers — no test ever reaches a real LLM (master prompt §12)
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_llm() -> Any:
    from tests.fakes import FakeLLMGateway

    return FakeLLMGateway()


@pytest.fixture
def fake_embedder() -> Any:
    from tests.fakes import FakeEmbedder

    return FakeEmbedder(dim=EMBEDDING_DIM)


# ---------------------------------------------------------------------------
# App + HTTP client
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def app(db: AsyncSession, fake_llm: Any, fake_embedder: Any) -> AsyncIterator[Any]:
    """The real FastAPI app with the DB session and provider gateways overridden."""
    from app.deps import get_db
    from app.main import create_app

    application = create_app()

    async def _override_db() -> AsyncIterator[AsyncSession]:
        yield db

    application.dependency_overrides[get_db] = _override_db

    # Override the provider gateways wherever they are injected.
    try:
        from app.services.llm.gateway import get_gateway
        from app.services.rag.embeddings import get_embedder

        application.dependency_overrides[get_gateway] = lambda: fake_llm
        application.dependency_overrides[get_embedder] = lambda: fake_embedder
    except ImportError:
        # Phase 0: these modules do not exist yet.
        pass

    yield application

    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: Any) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Users / auth helpers
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def user_factory(db: AsyncSession) -> Any:
    from app.models import User
    from app.security.passwords import hash_password

    async def _make(
        role: Role = Role.ANALYST,
        email: str | None = None,
        password: str = "correct-horse-battery-staple",
        is_active: bool = True,
    ) -> User:
        user = User(
            id=uuid.uuid4(),
            email=email or f"{role.value}-{uuid.uuid4().hex[:8]}@ministry.gov",
            full_name=f"Test {role.value.title()}",
            password_hash=hash_password(password),
            role=role,
            is_active=is_active,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user

    return _make


@pytest_asyncio.fixture
async def auth_headers(client: AsyncClient, user_factory: Any) -> Any:
    """Return a callable: role -> Authorization header for a freshly created user."""

    async def _headers(role: Role = Role.ANALYST) -> dict[str, str]:
        password = "correct-horse-battery-staple"
        user = await user_factory(role=role, password=password)
        resp = await client.post(
            "/api/auth/login", json={"email": user.email, "password": password}
        )
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    return _headers


# ---------------------------------------------------------------------------
# Content fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def sample_text() -> str:
    return (
        "The Ministry's 2026 strategic plan sets three priorities. "
        "First, diversify the national economy away from hydrocarbon revenue, targeting a "
        "non-oil GDP share of 65 percent by 2030.\n\n"
        "Second, establish a sovereign data residency framework requiring that all "
        "OFFICIAL-SENSITIVE government workloads remain within national borders.\n\n"
        "Third, raise the digital skills of the public workforce, with 40,000 civil servants "
        "trained in data literacy by the end of 2027."
    )


@pytest_asyncio.fixture
async def indexed_document(db: AsyncSession, fake_embedder: Any, sample_text: str) -> Any:
    """A fully indexed document with real chunks and (fake but deterministic) embeddings."""
    from app.enums import DocumentStatus, Language
    from app.models import Document, DocumentChunk

    doc = Document(
        id=uuid.uuid4(),
        title="Ministry Strategic Plan 2026",
        filename="strategy-2026.md",
        mime_type="text/markdown",
        storage_path="/tmp/strategy-2026.md",  # noqa: S108
        language=Language.EN,
        classification=Classification.INTERNAL,
        status=DocumentStatus.INDEXED,
        chunk_count=0,
    )
    db.add(doc)
    await db.flush()

    paragraphs = [p.strip() for p in sample_text.split("\n\n") if p.strip()]
    vectors = await fake_embedder.embed_documents(paragraphs)
    for i, (para, vec) in enumerate(zip(paragraphs, vectors, strict=True)):
        db.add(
            DocumentChunk(
                id=uuid.uuid4(),
                document_id=doc.id,
                chunk_index=i,
                content=para,
                token_count=len(para.split()),
                embedding=vec,
                meta={"source": "test"},
                classification=Classification.INTERNAL,
                language=Language.EN,
            )
        )
    doc.chunk_count = len(paragraphs)
    await db.commit()
    await db.refresh(doc)
    return doc


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC)
