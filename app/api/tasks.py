"""Action Tracker API: decisions turned into owned, tracked tasks. Mounted at /api/tasks.

Every read is clearance-filtered **in SQL** (docs/DECISIONS.md #15). Creating or updating an action
requires analyst clearance or above; a viewer sees the board but cannot change it. Every create and
update lands in the hash-chained audit log.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import client_ip, get_current_user, get_db, require_analyst
from app.enums import ActorType, TaskStatus, Urgency, classification_at_or_below
from app.exceptions import NotFoundError, PermissionDeniedError
from app.models import Task, User
from app.schemas.task import TaskCreate, TaskOut, TaskUpdate
from app.security.rbac import can_read, user_clearance
from app.services.audit_service import record_audit

log = structlog.get_logger(__name__)

router = APIRouter(tags=["tasks"])


def _out(t: Task) -> TaskOut:
    return TaskOut(
        id=t.id,
        title=t.title,
        description=t.description,
        status=TaskStatus(t.status),
        urgency=Urgency(t.urgency),
        owner=t.owner,
        progress=t.progress,
        due_date=t.due_date,
        classification=t.classification,
        created_by=t.created_by,
        source_insight_id=t.source_insight_id,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


@router.get(
    "",
    response_model=list[TaskOut],
    summary="List tracked actions (clearance-filtered)",
    description="Newest first. Filter by `status`. Only actions at or below your clearance are read.",
)
async def list_tasks(
    task_status: TaskStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[TaskOut]:
    stmt = (
        select(Task)
        .where(Task.classification.in_(classification_at_or_below(user_clearance(user))))
        .order_by(Task.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if task_status is not None:
        stmt = stmt.where(Task.status == task_status.value)
    rows = (await db.scalars(stmt)).all()
    return [_out(t) for t in rows]


@router.post(
    "",
    response_model=TaskOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a tracked action (analyst+)",
)
async def create_task(
    payload: TaskCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_analyst),
) -> TaskOut:
    if not can_read(user, payload.classification):
        raise PermissionDeniedError(
            "You cannot create an action classified above your own clearance.",
            detail={
                "required": payload.classification.value,
                "held": user_clearance(user).value,
            },
        )

    task = Task(
        id=uuid.uuid4(),
        title=payload.title,
        description=payload.description,
        status=TaskStatus.PENDING.value,
        urgency=payload.urgency.value,
        owner=payload.owner,
        progress=0,
        due_date=payload.due_date,
        classification=payload.classification,
        created_by=user.id,
        source_insight_id=payload.source_insight_id,
    )
    db.add(task)
    await db.flush()

    await record_audit(
        db,
        action="task.create",
        actor_type=ActorType.USER,
        actor_id=user.id,
        subject_type="task",
        subject_id=task.id,
        ip=client_ip(request),
        detail={"title": task.title, "urgency": task.urgency},
    )
    log.info("task_created", task_id=str(task.id), by=str(user.id))
    return _out(task)


@router.patch(
    "/{task_id}",
    response_model=TaskOut,
    summary="Update a tracked action (analyst+)",
    description=(
        "Change status, progress, owner, urgency, title, description or due date. Marking an action "
        "`done` sets progress to 100. An action above your clearance returns 404, not 403."
    ),
)
async def update_task(
    task_id: uuid.UUID,
    payload: TaskUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_analyst),
) -> TaskOut:
    task = await db.get(Task, task_id)
    # Clearance is part of the lookup: confirming an over-classified action exists would leak.
    if task is None or not can_read(user, task.classification):
        raise NotFoundError("No such action.", detail={"task_id": str(task_id)})

    changes = payload.model_dump(exclude_unset=True)
    if payload.title is not None:
        task.title = payload.title
    if payload.description is not None:
        task.description = payload.description
    if payload.status is not None:
        task.status = payload.status.value
    if payload.urgency is not None:
        task.urgency = payload.urgency.value
    if payload.owner is not None:
        task.owner = payload.owner
    if payload.progress is not None:
        task.progress = payload.progress
    if "due_date" in changes:
        task.due_date = payload.due_date
    # Keep progress and status coherent.
    if task.status == TaskStatus.DONE.value and task.progress < 100:
        task.progress = 100

    await db.flush()

    await record_audit(
        db,
        action="task.update",
        actor_type=ActorType.USER,
        actor_id=user.id,
        subject_type="task",
        subject_id=task.id,
        ip=client_ip(request),
        detail={"fields": sorted(changes), "status": task.status},
    )
    log.info("task_updated", task_id=str(task.id), fields=sorted(changes))
    # `updated_at` is set server-side on UPDATE, so its in-session value is expired; refresh it
    # explicitly rather than let the sync serializer trigger an implicit (illegal) async load.
    await db.refresh(task)
    return _out(task)
