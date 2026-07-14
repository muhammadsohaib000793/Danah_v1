"""Audit trail + hash-chain verification schemas (§7.7 #23)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, IPvAnyAddress, field_serializer

from app.enums import ActorType
from app.schemas.common import DanahModel


class AuditEntryOut(DanahModel):
    id: int
    ts: datetime
    actor_id: uuid.UUID | None = None
    actor_type: ActorType
    action: str
    subject_type: str | None = None
    subject_id: str | None = None
    # The column is INET, so the driver hands back an IPv4Address/IPv6Address, never a str.
    # Declared as a bare `str` this model rejected every audit entry that carried an IP — which
    # is every entry a human action produces — and the audit endpoints 500'd.
    ip: IPvAnyAddress | str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    entry_hash: str

    @field_serializer("ip")
    def _ip_as_str(self, value: IPvAnyAddress | str | None) -> str | None:
        return None if value is None else str(value)


class AuditVerifyResponse(DanahModel):
    """`valid: false` pinpoints the first entry whose recomputed hash disagrees with the stored
    one — i.e. the row that was tampered with, or the first row after a deletion."""

    valid: bool
    entries_checked: int
    broken_at_id: int | None = Field(
        default=None, description="audit_log.id of the first entry that fails verification"
    )
    broken_at_index: int | None = Field(
        default=None, description="0-based position of that entry in the walked chain"
    )
    reason: str | None = None
    first_id: int | None = None
    last_id: int | None = None
    verified_at: datetime
