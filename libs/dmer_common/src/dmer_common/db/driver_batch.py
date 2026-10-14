"""Count a finished document into its driver's open batch.

A driver's batch is its open ``driver_evaluation``; a document is counted into
it by a ``driver_evaluation_document`` marker row, once only, with
``completed_document_count`` incremented. The Decision Gateway decides the
batch when the documents counted into it cover every DMER Mercury still holds
for the driver (docs/development/stages/07-decision-gateway.md).

Two writers count documents in, each inside its own transaction:

- the Rule Engine, for a document whose rules were applied;
- Route To Manual Review, for a document that failed after Driver Lookup
  attached its driver -- finished, but never decided by the gateway. Without
  it, the driver's batch would wait for that document forever.

The open evaluation's row is locked first, so counting cannot interleave with
the gateway deciding and closing that batch: either the document is counted
before the gateway locks the row (the gateway then sees an extra document and
re-evaluates), or the gateway closes the batch first and the document starts
the driver's next batch (V0006) -- never counted into a batch already decided.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, Table, select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

metadata = MetaData()

# Created by V0001 (driver_evaluation) and V0004 (driver_evaluation_document).
driver_evaluation = Table(
    "driver_evaluation",
    metadata,
    Column(
        "id",
        PG_UUID(as_uuid=False),
        primary_key=True,
        server_default="gen_random_uuid()",
    ),
    Column("driver_key", PG_UUID(as_uuid=False), nullable=False),
    Column("open", Boolean, nullable=False),
    Column("completed_document_count", Integer, nullable=False),
)
driver_evaluation_document = Table(
    "driver_evaluation_document",
    metadata,
    Column("driver_evaluation_id", PG_UUID(as_uuid=False), primary_key=True),
    Column("document_id", PG_UUID(as_uuid=False), primary_key=True),
    Column("counted_at", DateTime(timezone=True), nullable=False),
)


async def count_into_open_batch(
    conn: AsyncConnection, *, driver_key: str, document_id: str, now: datetime
) -> tuple[bool, str]:
    """Count *document_id* into *driver_key*'s open batch, opening one if none
    is open. Returns ``(counted, driver_evaluation_id)``; ``counted`` is False
    when the document was already counted into that batch.

    Runs in the caller's transaction (*conn* must be inside one).
    """
    open_evaluation = (
        await conn.execute(
            select(driver_evaluation.c.id)
            .where(
                driver_evaluation.c.driver_key == driver_key,
                driver_evaluation.c.open.is_(True),
            )
            .with_for_update()
        )
    ).first()
    if open_evaluation is None:
        # No open batch: the previous one was decided and closed after Driver
        # Lookup attached this document (V0006). Open the driver's next one;
        # its expected count stays unset -- the gateway re-checks Mercury.
        evaluation_id = (
            await conn.execute(
                pg_insert(driver_evaluation)
                .values(driver_key=driver_key, open=True)  # status: WAITING
                .on_conflict_do_update(
                    index_elements=["driver_key"],
                    index_where=driver_evaluation.c.open,
                    set_={"driver_key": driver_key},  # no-op; returns the id
                )
                .returning(driver_evaluation.c.id)
            )
        ).scalar_one()
    else:
        evaluation_id = open_evaluation[0]
    marked = (
        await conn.execute(
            pg_insert(driver_evaluation_document)
            .values(
                driver_evaluation_id=evaluation_id,
                document_id=document_id,
                counted_at=now,
            )
            .on_conflict_do_nothing()
            .returning(driver_evaluation_document.c.document_id)
        )
    ).first()
    if marked is None:
        return False, str(evaluation_id)
    await conn.execute(
        driver_evaluation.update()
        .where(driver_evaluation.c.id == evaluation_id)
        .values(
            completed_document_count=driver_evaluation.c.completed_document_count + 1
        )
    )
    return True, str(evaluation_id)
