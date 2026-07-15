"""Seed the database: admin user, default sources, and three indexed sample documents.

Idempotent — safe to re-run. Existing rows are left alone rather than duplicated, so this can be
used to top up an environment after adding a new default source.

Document indexing needs an embedding provider. Without one (PENDING-CREDENTIALS mode) the sample
documents are still created and stored, left in `pending`, and the worker indexes them the moment
a key is added. Seeding never fabricates embeddings.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from typing import Any

import structlog

# The Windows console defaults to cp1252, which cannot encode the characters this script
# prints. Without this, `make seed` dies with a UnicodeEncodeError on Windows while working
# fine in the Linux container — a confusing failure that has nothing to do with seeding.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from scripts.sample_docs import SAMPLE_DOCUMENTS

from app.config import Settings, get_settings
from app.db import dispose_engine, get_session_factory
from app.enums import (
    ActorType,
    Classification,
    ConnectorKind,
    DocumentStatus,
    Language,
    Role,
    SourceType,
)
from app.logging import configure_logging
from app.models import Document, Source, User
from app.security.passwords import hash_password

log = structlog.get_logger("seed")


def default_sources(settings: Settings) -> list[dict[str, Any]]:
    """The four open sources from §8, configured from env — no keys required for any of them."""
    return [
        {
            "name": "World Bank Indicators",
            "type": SourceType.API,
            "connector": ConnectorKind.WORLDBANK,
            "config": {
                "countries": settings.watch_country_list,
                "indicators": settings.worldbank_indicator_list,
                "recent_years": 5,
            },
            "credibility_score": 0.95,
            "poll_interval_minutes": max(settings.default_poll_interval_minutes, 720),
        },
        {
            "name": "GDELT Global News",
            "type": SourceType.API,
            "connector": ConnectorKind.GDELT,
            "config": {
                "query_terms": settings.watch_query_term_list,
                "max_records": 60,
                "timespan": "24h",
            },
            "credibility_score": 0.60,
            "poll_interval_minutes": settings.default_poll_interval_minutes,
        },
        {
            "name": "News RSS Feeds",
            "type": SourceType.RSS,
            "connector": ConnectorKind.RSS,
            "config": {"feeds": settings.rss_feed_list},
            "credibility_score": 0.75,
            "poll_interval_minutes": settings.default_poll_interval_minutes,
        },
        {
            "name": "ReliefWeb Humanitarian Reports",
            "type": SourceType.API,
            "connector": ConnectorKind.RELIEFWEB,
            "config": {
                "countries": settings.reliefweb_country_list,
                "limit": 40,
            },
            "credibility_score": 0.85,
            "poll_interval_minutes": max(settings.default_poll_interval_minutes, 180),
        },
    ]


async def seed_admin(session: Any, settings: Settings) -> User:
    from sqlalchemy import select

    email = settings.admin_email.lower()
    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        log.info("admin_exists", email=email, role=existing.role.value)
        return existing

    password = settings.admin_initial_password.get_secret_value()
    if not password:
        raise SystemExit(
            "ADMIN_INITIAL_PASSWORD is not set in .env — cannot create the admin user."
        )

    admin = User(
        id=uuid.uuid4(),
        email=email,
        full_name="Ministry Administrator",
        password_hash=hash_password(password),
        role=Role.ADMIN,
        is_active=True,
    )
    session.add(admin)
    await session.flush()
    log.info("admin_created", email=email)
    print(f"  [ok] admin user created: {email}  (password from ADMIN_INITIAL_PASSWORD)")
    return admin


async def seed_approver(session: Any, settings: Settings) -> User | None:
    """Create the first executive, i.e. someone who can actually approve.

    Not a convenience. Nothing DANAH produces is ever published without a named human deciding
    to publish it, and every approval notification is addressed to `role=executive` — so a
    deployment with only an admin has an approval queue no one can clear and no one is told
    about. The logs say as much on every run: `email_no_recipients role=executive`. The whole
    point of the product is a human in the loop; seeding zero humans able to be that loop makes
    it a queue that fills and is never drained.

    The password is the same initial secret as the admin's and must be rotated on first login.
    """
    from sqlalchemy import select

    email = settings.approver_email.lower()
    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        log.info("approver_exists", email=email, role=existing.role.value)
        return existing

    password = settings.admin_initial_password.get_secret_value()
    if not password:
        return None

    approver = User(
        id=uuid.uuid4(),
        email=email,
        full_name="Ministry Executive",
        password_hash=hash_password(password),
        role=Role.EXECUTIVE,
        is_active=True,
    )
    session.add(approver)
    await session.flush()
    log.info("approver_created", email=email)
    print(f"  [ok] executive (approver) created: {email}  (same initial password — rotate it)")
    return approver


async def seed_demo_users(session: Any, settings: Settings) -> None:
    """Seed one analyst and one viewer so the classification boundary is demonstrable.

    admin/executive both clear OFFICIAL-SENSITIVE, so with only those two accounts you can never
    show the system *refusing* to disclose. The analyst (ceiling OFFICIAL) and the viewer
    (ceiling INTERNAL) exist precisely to be logged in as during a demo: the viewer's queries
    physically cannot return OFFICIAL-SENSITIVE rows — the exclusion is a SQL WHERE clause, not a
    UI toggle. Same initial password; rotate on first login.
    """
    from sqlalchemy import select

    password = settings.admin_initial_password.get_secret_value()
    if not password:
        return

    # A fuller demo roster so the login shows the ministry's ranks, not just four accounts. Every
    # account is real and its access is enforced server-side; the titles are labels on the same
    # four backend roles (admin, executive, analyst, viewer) and their three clearance ceilings.
    # Minister/DG collapse to EXECUTIVE (they can approve); advisor/analyst to ANALYST; focal/guest
    # to VIEWER. Same initial password as the rest; rotate on first login.
    wanted = [
        (settings.analyst_email.lower(), "Strategic Analyst", Role.ANALYST),
        (settings.viewer_email.lower(), "Entity Focal Point", Role.VIEWER),
        ("minister@ministry.gov", "Ministry Minister", Role.EXECUTIVE),
        ("dg@ministry.gov", "Director General", Role.EXECUTIVE),
        ("advisor@ministry.gov", "Senior Policy Advisor", Role.ANALYST),
        ("guest@ministry.gov", "Guest Viewer", Role.VIEWER),
    ]
    for email, full_name, role in wanted:
        existing = await session.scalar(select(User).where(User.email == email))
        if existing is not None:
            log.info("demo_user_exists", email=email, role=existing.role.value)
            continue
        session.add(
            User(
                id=uuid.uuid4(),
                email=email,
                full_name=full_name,
                password_hash=hash_password(password),
                role=role,
                is_active=True,
            )
        )
        await session.flush()
        log.info("demo_user_created", email=email, role=role.value)
        print(f"  [ok] {role.value} created: {email}  (same initial password — rotate it)")


async def seed_sources(session: Any, settings: Settings) -> int:
    from sqlalchemy import select

    created = 0
    for spec in default_sources(settings):
        existing = await session.scalar(select(Source).where(Source.name == spec["name"]))
        if existing is not None:
            continue
        session.add(Source(id=uuid.uuid4(), enabled=True, **spec))
        created += 1
        print(f"  [ok] source: {spec['name']}")

    await session.flush()
    return created


async def seed_documents(session: Any, settings: Settings, admin: User) -> tuple[int, int]:
    """Create + index the sample documents. Returns (created, indexed)."""
    from sqlalchemy import select

    from app.services.rag.indexer import index_document
    from app.services.rag.storage import write_document

    created = 0
    indexed = 0

    for spec in SAMPLE_DOCUMENTS:
        existing = await session.scalar(select(Document).where(Document.title == spec["title"]))
        if existing is not None:
            continue

        document_id = uuid.uuid4()
        data = spec["content"].encode("utf-8")
        storage_path = await write_document(
            data, filename=spec["filename"], document_id=document_id, settings=settings
        )

        session.add(
            Document(
                id=document_id,
                title=spec["title"],
                filename=spec["filename"],
                mime_type="text/markdown",
                storage_path=storage_path,
                language=Language.EN,
                classification=Classification(spec["classification"]),
                status=DocumentStatus.PENDING,
                uploaded_by=admin.id,
                chunk_count=0,
            )
        )
        await session.flush()
        created += 1
        print(f"  [ok] document: {spec['title']} [{spec['classification']}]")

        if settings.has_embedding_credentials:
            result = await index_document(session, document_id, settings=settings)
            if result.status is DocumentStatus.INDEXED:
                indexed += 1
                print(f"       indexed: {result.chunk_count} chunks")
            else:
                print(f"       [!] indexing failed: {result.error}")

    return created, indexed


async def _record_seed_audit(sources: int, documents: int, indexed: int) -> None:
    """Append a `system.seed` entry to the hash-chained audit log (Phase 4)."""
    from app.services.audit_service import record_audit

    factory = get_session_factory()
    async with factory() as session:
        await record_audit(
            session,
            action="system.seed",
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            subject_type="system",
            subject_id=None,
            detail={
                "sources_created": sources,
                "documents_created": documents,
                "documents_indexed": indexed,
            },
        )
        await session.commit()


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)

    rule = "-" * 62
    print(f"\nSeeding DANAH ({settings.app_env.value})\n{rule}")

    factory = get_session_factory()
    async with factory() as session:
        admin = await seed_admin(session, settings)
        await seed_approver(session, settings)
        await seed_demo_users(session, settings)
        sources = await seed_sources(session, settings)
        docs, indexed = await seed_documents(session, settings, admin)
        await session.commit()

    # The seed itself is recorded in the hash-chained audit log.
    await _record_seed_audit(sources, docs, indexed)

    print(rule)
    print(f"  sources created:   {sources}")
    print(f"  documents created: {docs}")

    if settings.has_embedding_credentials:
        print(f"  documents indexed: {indexed}")
        print("\nSeed complete. Chat is ready - ask about the diversification strategy.")
    else:
        print("  documents indexed: 0  (no embedding key)")
        print(
            "\n[PENDING-CREDENTIALS] The sample documents are stored but NOT indexed, because no\n"
            "embedding provider key is set. Embeddings are never faked. Add VOYAGE_API_KEY (or set\n"
            "EMBEDDING_PROVIDER=openai + OPENAI_API_KEY) to .env and re-run `make seed`.\n"
            "See FIRST_RUN.md."
        )

    print(f"\n  Log in as: {settings.admin_email}")
    print("  API docs:  http://localhost:8000/docs\n")

    await dispose_engine()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
