"""Knowledge base API (§7.7 #6-8): upload, list, semantic search. Mounted at /api/knowledge."""

from __future__ import annotations

import uuid
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.deps import get_config, get_current_user, get_db, require_analyst
from app.enums import Classification, DocumentStatus, Language, classification_at_or_below
from app.exceptions import InvalidRequestError, PermissionDeniedError
from app.models import Document, User
from app.schemas.knowledge import (
    DocumentOut,
    DocumentUploadResponse,
    SearchHit,
    SearchRequest,
    SearchResponse,
)
from app.security.rbac import user_clearance
from app.services.rag.embeddings import Embedder, get_embedder
from app.services.rag.indexer import guess_mime_type
from app.services.rag.retriever import Retriever
from app.services.rag.storage import write_document

log = structlog.get_logger(__name__)

router = APIRouter(tags=["knowledge"])


@router.post(
    "/documents",
    response_model=DocumentUploadResponse,
    status_code=202,
    summary="Upload a document for indexing (analyst+)",
    description=(
        "Accepts pdf/docx/txt/md/html up to `MAX_UPLOAD_SIZE_MB`. Returns immediately with status "
        "`pending`; extraction, chunking and embedding run in the background. Poll "
        "`GET /api/knowledge/documents` until the document reads `indexed`."
    ),
)
async def upload_document(
    file: UploadFile = File(..., description="The document to index"),
    title: str | None = Form(default=None, description="Defaults to the filename"),
    classification: Classification = Form(
        default=Classification.INTERNAL,
        description="Sensitivity tier. Chunks inherit it, and retrieval filters on it.",
    ),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_analyst),
    settings: Settings = Depends(get_config),
) -> DocumentUploadResponse:
    filename = file.filename or "document"
    suffix = Path(filename).suffix.lower().lstrip(".")

    if suffix not in settings.allowed_upload_extension_set:
        raise InvalidRequestError(
            f"'.{suffix}' files are not accepted.",
            code="unsupported_file_type",
            detail={"allowed": sorted(settings.allowed_upload_extension_set)},
        )

    # A user may not create data above their own clearance — otherwise an analyst could upload an
    # OFFICIAL_SENSITIVE document and then be unable to read back what they just filed.
    if classification not in classification_at_or_below(user_clearance(user)):
        raise PermissionDeniedError(
            "You cannot classify a document above your own clearance.",
            detail={"requested": classification.value, "held": user_clearance(user).value},
        )

    data = await file.read()
    if not data:
        raise InvalidRequestError("The uploaded file is empty.", code="empty_upload")
    if len(data) > settings.max_upload_size_bytes:
        raise InvalidRequestError(
            f"The file exceeds the {settings.max_upload_size_mb} MB limit.",
            code="file_too_large",
            detail={"size_bytes": len(data), "limit_bytes": settings.max_upload_size_bytes},
        )

    document_id = uuid.uuid4()
    storage_path = await write_document(
        data, filename=filename, document_id=document_id, settings=settings
    )

    document = Document(
        id=document_id,
        title=(title or Path(filename).stem).strip()[:500],
        filename=filename,
        mime_type=file.content_type or guess_mime_type(filename),
        storage_path=storage_path,
        language=Language.EN,  # corrected from the extracted text during indexing
        classification=classification,
        status=DocumentStatus.PENDING,
        uploaded_by=user.id,
        chunk_count=0,
    )
    db.add(document)
    await db.flush()

    await _enqueue_indexing(document_id, settings)

    log.info(
        "document_uploaded",
        document_id=str(document_id),
        uploaded_by=str(user.id),
        classification=classification.value,
        size_bytes=len(data),
        # The document's content is never logged.
    )

    return DocumentUploadResponse(
        id=document.id,
        title=document.title,
        filename=document.filename,
        status=document.status,
    )


async def _enqueue_indexing(document_id: uuid.UUID, settings: Settings) -> None:
    """Hand the document to the ARQ worker.

    If Redis is unreachable the upload still succeeds and the row stays `pending`: losing the
    queue must not lose the user's file. `docs/RUNBOOK.md` documents the re-drive.
    """
    from arq import create_pool
    from arq.connections import RedisSettings

    from app.logging import get_request_id

    try:
        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        # `request_id`, not `_request_id`: ARQ forwards any keyword it does not reserve straight
        # to the task, so the underscored form was passed to `embed_document` as an argument it
        # does not accept and the job died with a TypeError before doing any work.
        await pool.enqueue_job("embed_document", str(document_id), request_id=get_request_id())
        await pool.aclose()
    except Exception as exc:
        log.error(
            "enqueue_embed_document_failed",
            document_id=str(document_id),
            error=str(exc),
            hint="Document stays 'pending'; re-drive with scripts/reindex or POST again.",
        )


@router.get(
    "/documents",
    response_model=list[DocumentOut],
    summary="List documents and their indexing status",
    description="Only documents at or below the caller's clearance are returned.",
)
async def list_documents(
    status: DocumentStatus | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[DocumentOut]:
    allowed = classification_at_or_below(user_clearance(user))

    stmt = (
        select(Document)
        .where(Document.classification.in_(allowed))
        .order_by(Document.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if status is not None:
        stmt = stmt.where(Document.status == status)

    documents = (await db.scalars(stmt)).all()
    return [DocumentOut.model_validate(d) for d in documents]


@router.post(
    "/search",
    response_model=SearchResponse,
    summary="Semantic search over the corpus (analyst+)",
    description=(
        "Hybrid vector + keyword search with reciprocal rank fusion. Exposed for debugging and "
        "for the UI's source explorer — chat uses the same retriever internally."
    ),
)
async def search(
    payload: SearchRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_analyst),
    embedder: Embedder = Depends(get_embedder),
    settings: Settings = Depends(get_config),
) -> SearchResponse:
    retriever = Retriever(embedder, settings)
    hits = await retriever.retrieve(
        db,
        payload.query,
        k=payload.k,
        classification_ceiling=user_clearance(user),
        language=payload.language,
        hybrid=payload.hybrid,
    )

    return SearchResponse(
        query=payload.query,
        hits=[
            SearchHit(
                chunk_id=h.chunk_id,
                document_id=h.document_id,
                document_title=h.document_title,
                chunk_index=h.chunk_index,
                content=h.content,
                score=round(h.score, 4),
                vector_score=round(h.vector_score, 4) if h.vector_score is not None else None,
                keyword_score=round(h.keyword_score, 4) if h.keyword_score is not None else None,
                classification=h.classification,
            )
            for h in hits
        ],
        total=len(hits),
        hybrid=settings.hybrid_search_enabled if payload.hybrid is None else payload.hybrid,
    )


@router.get(
    "/documents/count",
    response_model=dict[str, int],
    summary="Document counts by status",
)
async def document_counts(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, int]:
    allowed = classification_at_or_below(user_clearance(user))
    rows = (
        await db.execute(
            select(Document.status, func.count(Document.id))
            .where(Document.classification.in_(allowed))
            .group_by(Document.status)
        )
    ).all()
    counts = {status.value: 0 for status in DocumentStatus}
    for status, count in rows:
        counts[status.value] = int(count)
    return counts
