"""``mercury_outbox`` -- the intent to write an outcome back to Mercury.

See docs/development/stages/08-post-processing.md (architecture v2.1 4.5).
Post-Processing never calls Mercury: it writes one outbox row per intended
Mercury operation in the **same transaction** as the decision, so a decision
can never exist without someone trying to deliver it. The Outbox Publisher
(4.6.1) drains the rows.

Operations per decided document:

- ``MAP_DRIVER`` -- the driver was matched from the page's licence (I-11:
  the AI may map it automatically);
- ``CREATE_CASE`` -- on every AI decision; the publisher checks live whether
  the driver has an open case and creates one only when there is none,
  otherwise attaches the DMER to it (I-13);
- ``MARK_DUPLICATE`` -- a copy rejected as a duplicate (Mercury status
  ``Rejected``, I-7), only ever emitted for a clean pass;
- ``UPDATE_OUTCOME`` -- always: the outcome and the reasoning (I-1, I-5,
  I-12, I-13: the reason is always in the comment).

A fallback (the pipeline could not decide) gets ``UPDATE_OUTCOME`` only: IN,
with "AI could not process" and the reason.

The publisher applies a document's rows in :data:`OPERATION_ORDER`, so the
driver and case exist before the outcome is written.

**Payloads are provisional**: Mercury has not shared its POST/PUT structure
or idempotency behaviour yet (question M-6). Everything the publisher needs
is here; the field names may change once M-6 is answered.

Comments carry clinical content (the rule engine's reason). They go to the
database and to Mercury, never to logs: log lines carry ``document_id``,
``operation`` and ``idempotency_key`` only.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import Column, DateTime, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from ..telemetry import get_logger

_log = get_logger(__name__)

metadata = MetaData()


class OutboxOperation(str, enum.Enum):
    """``mercury_outbox.operation`` (enum ``mercury_outbox_operation``, V0001)."""

    MAP_DRIVER = "MAP_DRIVER"
    CREATE_CASE = "CREATE_CASE"
    MARK_DUPLICATE = "MARK_DUPLICATE"
    UPDATE_OUTCOME = "UPDATE_OUTCOME"


# The order the publisher applies one document's rows in: the driver and the
# case first, the outcome last.
OPERATION_ORDER: Final = (
    OutboxOperation.MAP_DRIVER,
    OutboxOperation.CREATE_CASE,
    OutboxOperation.MARK_DUPLICATE,
    OutboxOperation.UPDATE_OUTCOME,
)

# I-1: the comment every fallback outcome carries.
AI_COULD_NOT_PROCESS: Final = "AI could not process"

mercury_outbox = Table(
    "mercury_outbox",
    metadata,
    Column(
        "id",
        PG_UUID(as_uuid=False),
        primary_key=True,
        server_default="gen_random_uuid()",
    ),
    Column("document_id", PG_UUID(as_uuid=False), nullable=False),
    Column(
        "operation",
        PG_ENUM(
            *(o.value for o in OutboxOperation),
            name="mercury_outbox_operation",
            create_type=False,
        ),
        nullable=False,
    ),
    Column("payload", JSONB, nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column(
        "status",
        PG_ENUM(
            "PENDING", "SENT", "FAILED", name="mercury_outbox_status", create_type=False
        ),
        nullable=False,
    ),
    Column("attempt_count", Integer, nullable=False),
    Column("next_attempt_at", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("sent_at", DateTime(timezone=True)),
    Column("mercury_response", JSONB),
)


@dataclass(frozen=True)
class OutboxRow:
    """One intended Mercury operation for one document."""

    document_id: str
    operation: OutboxOperation
    payload: dict[str, Any]  # carries the comment -- clinical content, never log
    idempotency_key: str


def idempotency_key(source_id: str, operation: OutboxOperation) -> str:
    """Deterministic per decision (or per document, for a fallback without a
    decision row) and operation, so the same outcome is never queued twice."""
    return f"{source_id}:{operation.value}"


def decision_comment(outcome_code: str, reason: Mapping[str, Any] | None) -> str:
    """The reasoning written back to Mercury with an AI outcome: the outcome,
    the rule engine's reason, and the gateway's notes (attached report,
    cut-off, duplicates, differing fields, proposed driver)."""
    reason = reason or {}
    parts = [f"AI outcome: {outcome_code}."]
    # Forwarded duplicates carry the highest-priority copy's reason, which is
    # the one actually applied.
    rule_reason = str(
        reason.get("applied_rule") or reason.get("rule_reason") or ""
    ).strip()
    if rule_reason:
        parts.append(f"Reason: {rule_reason.rstrip('.')}.")
    differing = reason.get("differing_fields")
    if differing:
        parts.append(f"Fields that differ: {', '.join(sorted(differing))}.")
    parts.extend(str(note).strip() for note in reason.get("notes") or () if note)
    return " ".join(parts)


def fallback_comment(*reasons: str) -> str:
    """The comment on a fallback IN: "AI could not process" plus the reason(s)."""
    detail = " ".join(r.strip() for r in reasons if r and r.strip())
    return f"{AI_COULD_NOT_PROCESS}. {detail}".strip()


def decision_rows(
    *,
    decision_id: str,
    document_id: str,
    document_guid: str,
    outcome_code: str,
    decided_by: str,
    reason: Mapping[str, Any] | None,
    mercury_driver_id: str | None,
    is_duplicate: bool = False,
    duplicate_of_document_guid: str | None = None,
    proposed_mercury_driver_id: str | None = None,
    proposed_driver: bool = False,
) -> list[OutboxRow]:
    """The outbox rows for one AI decision, in :data:`OPERATION_ORDER`."""
    comment = decision_comment(outcome_code, reason)
    base = {"document_guid": document_guid, "comment": comment}
    rows = []

    def add(operation: OutboxOperation, **payload: Any) -> None:
        rows.append(
            OutboxRow(
                document_id=document_id,
                operation=operation,
                payload={**base, **payload},
                idempotency_key=idempotency_key(decision_id, operation),
            )
        )

    if proposed_driver:
        add(OutboxOperation.MAP_DRIVER, mercury_driver_id=proposed_mercury_driver_id)
    add(OutboxOperation.CREATE_CASE, mercury_driver_id=mercury_driver_id)
    if is_duplicate:
        add(
            OutboxOperation.MARK_DUPLICATE,
            status="Rejected",
            duplicate_of_document_guid=duplicate_of_document_guid,
        )
    add(
        OutboxOperation.UPDATE_OUTCOME, outcome_code=outcome_code, decided_by=decided_by
    )
    return rows


def fallback_rows(
    *, source_id: str, document_id: str, document_guid: str, comment: str
) -> list[OutboxRow]:
    """A fallback outcome: ``UPDATE_OUTCOME`` with IN only."""
    operation = OutboxOperation.UPDATE_OUTCOME
    return [
        OutboxRow(
            document_id=document_id,
            operation=operation,
            payload={
                "document_guid": document_guid,
                "comment": comment,
                "outcome_code": "IN",
                "decided_by": "FALLBACK",
            },
            idempotency_key=idempotency_key(source_id, operation),
        )
    ]


async def queue_outbox_rows(
    conn: AsyncConnection, rows: Iterable[OutboxRow], *, now: datetime
) -> int:
    """Insert *rows* as PENDING, due now, inside the caller's transaction.

    A row whose idempotency key already exists is skipped (the same intent is
    already queued). Returns how many were inserted. Logs one line per row.
    """
    rows = list(rows)
    if not rows:
        return 0
    inserted = (
        await conn.execute(
            pg_insert(mercury_outbox)
            .values(
                [
                    {
                        "document_id": row.document_id,
                        "operation": row.operation.value,
                        "payload": row.payload,
                        "idempotency_key": row.idempotency_key,
                        "status": "PENDING",
                        "attempt_count": 0,
                        "next_attempt_at": now,
                    }
                    for row in rows
                ]
            )
            .on_conflict_do_nothing(index_elements=[mercury_outbox.c.idempotency_key])
            .returning(mercury_outbox.c.idempotency_key)
        )
    ).scalars()
    queued = set(inserted)
    for row in rows:
        _log.info(
            (
                "outbox row queued"
                if row.idempotency_key in queued
                else "outbox row exists"
            ),
            extra={
                "document_id": row.document_id,
                "operation": row.operation.value,
                "idempotency_key": row.idempotency_key,
            },
        )
    return len(queued)
