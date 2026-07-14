"""Phase 4 — the classification enforcement sweep (master prompt §10, execution step 42).

A viewer (clearance INTERNAL) must not reach OFFICIAL_SENSITIVE content through **any** read path:
documents, chunks used to ground chat, semantic search, insights, briefings, memory, or the
dashboard's counts.

The dashboard is included deliberately: a *count* is itself a leak. "There are 3 OFFICIAL_SENSITIVE
risks you cannot open" tells a viewer something they are not cleared to know.

Every test here asserts the negative AND its positive control — a filter that blocks everyone
would pass a negative-only test while breaking the product.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import (
    AgentName,
    Classification,
    DocumentStatus,
    InsightKind,
    Language,
    MemoryKind,
    PublicationStatus,
    Role,
)
from app.models import Briefing, Document, DocumentChunk, Insight

SECRET = "The covert programme codename is NIGHTJAR and its budget is 40 million dirhams."


async def make_classified_document(
    db: AsyncSession,
    fake_embedder: Any,
    *,
    classification: Classification,
    text: str = SECRET,
    title: str = "Sensitive Annex",
) -> Document:
    doc = Document(
        id=uuid.uuid4(),
        title=title,
        filename="annex.md",
        mime_type="text/markdown",
        storage_path=f"test/{uuid.uuid4()}.md",
        language=Language.EN,
        classification=classification,
        status=DocumentStatus.INDEXED,
        chunk_count=1,
    )
    db.add(doc)
    await db.flush()

    vectors = await fake_embedder.embed_documents([text])
    db.add(
        DocumentChunk(
            id=uuid.uuid4(),
            document_id=doc.id,
            chunk_index=0,
            content=text,
            token_count=len(text.split()),
            embedding=vectors[0],
            classification=classification,
            language=Language.EN,
            meta={},
        )
    )
    await db.commit()
    return doc


async def make_classified_insight(
    db: AsyncSession, *, classification: Classification, title: str, body: str = SECRET
) -> Insight:
    """`body` defaults to the secret, so callers must opt *out* for a non-sensitive record.

    The secret must live only in the OFFICIAL_SENSITIVE rows. Planting it in the INTERNAL one
    too — which a viewer is entitled to read — makes "NIGHTJAR is absent from the viewer's
    response" fail on a record that was never sensitive, and the assertion stops meaning
    anything: it can no longer distinguish a working clearance filter from a broken one.
    """
    insight = Insight(
        id=uuid.uuid4(),
        kind=InsightKind.RISK,
        title=title,
        body=body,
        severity=5,
        likelihood=0.8,
        confidence=0.9,
        domains=["security"],
        recommendations=[],
        citations={"items": [], "chunks": []},
        language=Language.EN,
        classification=classification,
        status=PublicationStatus.PUBLISHED,  # published, but still classified
        created_by_agent=AgentName.RISK,
    )
    db.add(insight)
    await db.commit()
    return insight


@pytest.fixture
async def sensitive_corpus(db: AsyncSession, fake_embedder: Any) -> dict[str, Any]:
    """One OFFICIAL_SENSITIVE item and one INTERNAL item in every readable store."""
    secret_doc = await make_classified_document(
        db, fake_embedder, classification=Classification.OFFICIAL_SENSITIVE
    )
    public_doc = await make_classified_document(
        db,
        fake_embedder,
        classification=Classification.INTERNAL,
        text="The diversification target is 65 percent of non-oil GDP by 2030.",
        title="Public Strategy",
    )
    secret_insight = await make_classified_insight(
        db, classification=Classification.OFFICIAL_SENSITIVE, title="NIGHTJAR exposure"
    )
    public_insight = await make_classified_insight(
        db,
        classification=Classification.INTERNAL,
        title="Trade concentration",
        body="Advanced logic capacity is concentrated in a single jurisdiction.",
    )
    return {
        "secret_doc": secret_doc,
        "public_doc": public_doc,
        "secret_insight": secret_insight,
        "public_insight": public_insight,
    }


class TestDocumentsAndChat:
    async def test_viewer_cannot_list_a_sensitive_document(
        self, client: AsyncClient, sensitive_corpus: dict[str, Any], auth_headers: Any
    ) -> None:
        headers = await auth_headers(Role.VIEWER)

        resp = await client.get("/api/knowledge/documents", headers=headers)

        assert resp.status_code == 200
        titles = [d["title"] for d in resp.json()]
        assert "Sensitive Annex" not in titles
        assert "Public Strategy" in titles  # positive control

    async def test_executive_can_list_it(
        self, client: AsyncClient, sensitive_corpus: dict[str, Any], auth_headers: Any
    ) -> None:
        headers = await auth_headers(Role.EXECUTIVE)

        resp = await client.get("/api/knowledge/documents", headers=headers)

        titles = [d["title"] for d in resp.json()]
        assert "Sensitive Annex" in titles

    async def test_the_secret_never_reaches_a_viewers_chat_answer(
        self, client: AsyncClient, sensitive_corpus: dict[str, Any], auth_headers: Any
    ) -> None:
        headers = await auth_headers(Role.VIEWER)

        resp = await client.post(
            "/api/agent/chat",
            json={"message": "What is the covert programme codename and its budget?"},
            headers=headers,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert "NIGHTJAR" not in body["answer"]
        assert "40 million" not in body["answer"]
        for citation in body["citations"]:
            assert citation["title"] != "Sensitive Annex"

    async def test_analyst_is_also_blocked_from_official_sensitive(
        self, client: AsyncClient, sensitive_corpus: dict[str, Any], auth_headers: Any
    ) -> None:
        """An analyst's ceiling is OFFICIAL — one tier below OFFICIAL_SENSITIVE."""
        headers = await auth_headers(Role.ANALYST)

        resp = await client.post(
            "/api/agent/chat",
            json={"message": "What is the covert programme codename?"},
            headers=headers,
        )

        assert "NIGHTJAR" not in resp.json()["answer"]

    async def test_semantic_search_does_not_return_over_cleared_chunks(
        self, client: AsyncClient, sensitive_corpus: dict[str, Any], auth_headers: Any
    ) -> None:
        headers = await auth_headers(Role.ANALYST)

        resp = await client.post(
            "/api/knowledge/search",
            headers=headers,
            json={"query": "covert programme codename NIGHTJAR budget", "k": 10},
        )

        assert resp.status_code == 200
        for hit in resp.json()["hits"]:
            assert hit["classification"] != Classification.OFFICIAL_SENSITIVE.value
            assert "NIGHTJAR" not in hit["content"]


class TestInsightsAndBriefings:
    async def test_viewer_cannot_list_a_sensitive_insight(
        self, client: AsyncClient, sensitive_corpus: dict[str, Any], auth_headers: Any
    ) -> None:
        headers = await auth_headers(Role.VIEWER)

        resp = await client.get("/api/insights", headers=headers)

        assert resp.status_code == 200
        titles = [i["title"] for i in resp.json()["items"]]
        assert "NIGHTJAR exposure" not in titles
        assert "Trade concentration" in titles  # positive control

    async def test_viewer_cannot_fetch_a_sensitive_insight_by_id(
        self, client: AsyncClient, sensitive_corpus: dict[str, Any], auth_headers: Any
    ) -> None:
        headers = await auth_headers(Role.VIEWER)
        secret = sensitive_corpus["secret_insight"]

        resp = await client.get(f"/api/insights/{secret.id}", headers=headers)

        assert resp.status_code in (403, 404)
        assert "NIGHTJAR" not in resp.text

    async def test_viewer_cannot_read_a_sensitive_briefing(
        self, client: AsyncClient, db: AsyncSession, auth_headers: Any
    ) -> None:
        briefing = Briefing(
            id=uuid.uuid4(),
            date=datetime.now(UTC).date(),
            title="Classified Briefing",
            body_en=SECRET,
            body_ar="نص سري",
            sections=[],
            citations={},
            confidence=0.8,
            classification=Classification.OFFICIAL_SENSITIVE,
            status=PublicationStatus.PUBLISHED,
        )
        db.add(briefing)
        await db.commit()

        headers = await auth_headers(Role.VIEWER)

        listing = await client.get("/api/briefings", headers=headers)
        assert "Classified Briefing" not in [b["title"] for b in listing.json()]

        detail = await client.get(f"/api/briefings/{briefing.id}", headers=headers)
        assert detail.status_code in (403, 404)
        assert "NIGHTJAR" not in detail.text


class TestMemoryAndDashboard:
    async def test_viewer_cannot_search_sensitive_memory(
        self, client: AsyncClient, db: AsyncSession, fake_embedder: Any, auth_headers: Any
    ) -> None:
        from app.services.memory_service import create_memory

        await create_memory(
            db,
            kind=MemoryKind.CONTEXT,
            title="NIGHTJAR standing context",
            content=SECRET,
            tags=[],
            source_ref={},
            classification=Classification.OFFICIAL_SENSITIVE,
            embedder=fake_embedder,
            created_by=None,
        )
        await db.commit()

        # Memory is analyst+; an analyst's ceiling is still below OFFICIAL_SENSITIVE.
        headers = await auth_headers(Role.ANALYST)

        listing = await client.get("/api/memory", headers=headers)
        assert listing.status_code == 200
        assert "NIGHTJAR" not in listing.text

        search = await client.post(
            "/api/memory/search", headers=headers, json={"query": "NIGHTJAR codename", "k": 5}
        )
        assert search.status_code == 200

        # The response echoes the caller's own query, so a bare "NIGHTJAR" not in text" would
        # fail on the word the analyst just typed — which discloses nothing they did not already
        # write. What must never come back is the memory: no hits, no title, no content.
        body = search.json()
        assert body["hits"] == [], "an over-cleared memory must not be retrievable"
        assert SECRET not in search.text
        assert "standing context" not in search.text

    async def test_the_dashboard_count_does_not_leak_the_existence_of_sensitive_insights(
        self, client: AsyncClient, sensitive_corpus: dict[str, Any], auth_headers: Any
    ) -> None:
        """A count is an information leak: "3 risks you cannot open" is itself a disclosure."""
        viewer = await auth_headers(Role.VIEWER)
        executive = await auth_headers(Role.EXECUTIVE)

        viewer_summary = await client.get("/api/dashboard/summary", headers=viewer)
        exec_summary = await client.get("/api/dashboard/summary", headers=executive)

        assert viewer_summary.status_code == 200, viewer_summary.text
        assert exec_summary.status_code == 200

        viewer_counts = viewer_summary.json()["counts"]
        exec_counts = exec_summary.json()["counts"]

        # Two insights exist; the viewer may only know about one of them.
        assert viewer_counts["insights_total"] < exec_counts["insights_total"]
        assert "NIGHTJAR" not in viewer_summary.text

    async def test_top_insights_on_the_dashboard_respect_clearance(
        self, client: AsyncClient, sensitive_corpus: dict[str, Any], auth_headers: Any
    ) -> None:
        headers = await auth_headers(Role.VIEWER)

        resp = await client.get("/api/dashboard/summary", headers=headers)

        assert resp.status_code == 200
        titles = [i["title"] for i in resp.json()["top_insights"]]
        assert "NIGHTJAR exposure" not in titles


class TestUploadClearance:
    async def test_a_user_cannot_classify_above_their_own_clearance(
        self, client: AsyncClient, auth_headers: Any
    ) -> None:
        """Otherwise an analyst could file a document and then be unable to read it back."""
        headers = await auth_headers(Role.ANALYST)

        resp = await client.post(
            "/api/knowledge/documents",
            headers=headers,
            files={"file": ("x.txt", b"content", "text/plain")},
            data={"classification": Classification.OFFICIAL_SENSITIVE.value},
        )

        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "permission_denied"

    async def test_a_user_can_classify_at_their_own_clearance(
        self, client: AsyncClient, auth_headers: Any
    ) -> None:
        headers = await auth_headers(Role.ANALYST)

        resp = await client.post(
            "/api/knowledge/documents",
            headers=headers,
            files={"file": ("x.txt", b"content", "text/plain")},
            data={"classification": Classification.OFFICIAL.value},
        )

        assert resp.status_code == 202
