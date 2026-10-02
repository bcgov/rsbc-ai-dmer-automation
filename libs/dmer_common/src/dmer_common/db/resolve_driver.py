"""DB unit of work for the Resolve Driver activity.

See docs/development/stages/03-document-orchestration.md ("Activity: Resolve
Driver"). Like Normalize and Rule Engine, every write for one document goes
through one connection holding a per-document advisory lock.

A resolved document, in one transaction:

- upserts ``driver`` on its canonical licence (names and Mercury id refreshed);
- attaches ``driver_key`` to the document, with how it was resolved and
  whether the page's licence disagreed with Mercury's driver;
- creates or attaches the driver's open ``driver_evaluation`` and **sets**
  ``expected_document_count`` from Mercury (set, never incremented, so a
  retry rewrites the same value);
- marks the ``DRIVER_LOOKUP`` stage run ``SUCCEEDED``.

A document that can't be resolved goes to ``MANUAL_REVIEW``, with the reason
as the stage run's ``error_code`` and ``dmer_document.manual_review_reason``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ._advisory import advisory_lock_session
from .dmer_document import _pipeline_status_enum
from .dmer_stage_run import dmer_stage_run
from .driver import driver, normalize_licence_number

metadata = MetaData()

# The dmer_document columns this stage reads and writes (V0001, V0005, V0006).
_document = Table(
    "dmer_document",
    metadata,
    Column("id", PG_UUID(as_uuid=False), primary_key=True),
    Column("driver_key", PG_UUID(as_uuid=False)),
    Column("pipeline_status", _pipeline_status_enum),
    Column("attempt_count", Integer),
    Column("updated_at", DateTime(timezone=True)),
    Column("driver_resolved_by", Text),
    Column("licence_mismatch", Boolean),
    Column("manual_review_reason", Text),
)
_extraction = Table(
    "dmer_extraction",
    metadata,
    Column("document_id", PG_UUID(as_uuid=False), primary_key=True),
    Column("licence_number_read", Text),
)
_driver_evaluation = Table(
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
    Column("expected_document_count", Integer),
    Column("last_mercury_check_at", DateTime(timezone=True)),
)

# How the driver was decided (dmer_document.driver_resolved_by).
MERCURY_SUPPLIED: Final = "MERCURY_SUPPLIED"
LICENCE_LOOKUP: Final = "LICENCE_LOOKUP"

# Why a document went to manual review (the stage run's error_code). The
# licence and Mercury details themselves are never stored here or logged.
LICENCE_UNREADABLE: Final = "LICENCE_UNREADABLE"
DRIVER_NOT_FOUND: Final = "DRIVER_NOT_FOUND"
DRIVER_AMBIGUOUS: Final = "DRIVER_AMBIGUOUS"
MANUAL_REVIEW_REASONS: Final = {
    LICENCE_UNREADABLE: "No readable licence on the page and none from Mercury.",
    DRIVER_NOT_FOUND: "Mercury has no driver with the page's licence.",
    DRIVER_AMBIGUOUS: "Mercury has more than one driver with the page's licence.",
}

_STAGE = "DRIVER_LOOKUP"
_RUNNING = "RUNNING"
_SUCCEEDED = "SUCCEEDED"
_FAILED = "FAILED"
_READY = "EXTRACTED"
_NOT_READY = {"RECEIVED", "DOWNLOADED", "EXTRACTING"}


class ResolveDriverBusyError(RuntimeError):
    """Another Resolve Driver session already holds the document lock."""

    def __init__(self) -> None:
        super().__init__("Driver resolution is already running for this document.")


class ResolveDriverNotReadyError(RuntimeError):
    """The document hasn't been extracted yet."""

    def __init__(self) -> None:
        super().__init__("Driver resolution is not ready for this document.")


class ResolveDriverStateError(RuntimeError):
    """Missing document or inconsistent Resolve Driver state."""

    def __init__(self) -> None:
        super().__init__("Driver resolution state is invalid for this operation.")


@dataclass(frozen=True)
class ResolveOutcome:
    """A committed result: the driver, or the manual-review reason."""

    driver_key: str | None
    manual_review_reason: str | None = None


@dataclass(frozen=True)
class ResolveDriverRun:
    run_id: int
    attempt_no: int
    driver_key: str | None  # Mercury-supplied, set by Ingest
    driver_licence: str | None  # that driver's canonical licence
    licence_read: str | None  # canonical licence read off the page
    completed: ResolveOutcome | None = None


@dataclass(frozen=True)
class MercuryDriver:
    """The fields of Mercury's driver this stage stores."""

    licence_number: str
    mercury_driver_id: str | None
    first_name: str | None
    last_name: str | None


class ResolveDriverRepository:
    """Resolve Driver DB operations bound to one dedicated async connection."""

    def __init__(self, conn: AsyncConnection, *, document_id: str) -> None:
        self._conn = conn
        self._document_id = document_id

    def _check_session(self) -> None:
        # A transparently reconnected connection would not own our advisory lock.
        if self._conn.closed or self._conn.invalidated:
            raise ResolveDriverBusyError()

    async def start_or_resume(self, *, now: datetime) -> ResolveDriverRun:
        """Start a new attempt, or return the committed outcome."""
        self._check_session()
        async with self._conn.begin():
            doc = (
                await self._conn.execute(
                    select(_document.c.pipeline_status, _document.c.driver_key)
                    .where(_document.c.id == self._document_id)
                    .with_for_update()
                )
            ).first()
            if doc is None:
                raise ResolveDriverStateError()
            status, driver_key = doc
            if status in _NOT_READY:
                raise ResolveDriverNotReadyError()

            last = (
                await self._conn.execute(
                    select(
                        dmer_stage_run.c.id,
                        dmer_stage_run.c.attempt_no,
                        dmer_stage_run.c.status,
                        dmer_stage_run.c.error_code,
                    )
                    .where(
                        dmer_stage_run.c.document_id == self._document_id,
                        dmer_stage_run.c.stage == _STAGE,
                        dmer_stage_run.c.status != _RUNNING,
                    )
                    .order_by(
                        dmer_stage_run.c.attempt_no.desc(), dmer_stage_run.c.id.desc()
                    )
                    .limit(1)
                )
            ).first()
            if last is not None and last[2] == _SUCCEEDED and driver_key:
                return ResolveDriverRun(
                    last[0], last[1], driver_key, None, None, ResolveOutcome(driver_key)
                )
            if status == "MANUAL_REVIEW":
                if last is not None and last[3] in MANUAL_REVIEW_REASONS:
                    return ResolveDriverRun(
                        last[0],
                        last[1],
                        None,
                        None,
                        None,
                        ResolveOutcome(None, last[3]),
                    )
                raise ResolveDriverStateError()  # sent to review by another stage
            if status != _READY:
                raise ResolveDriverStateError()

            driver_licence = None
            if driver_key:
                driver_licence = (
                    await self._conn.execute(
                        select(driver.c.licence_number).where(
                            driver.c.driver_key == driver_key
                        )
                    )
                ).scalar_one_or_none()
            licence_read = (
                await self._conn.execute(
                    select(_extraction.c.licence_number_read).where(
                        _extraction.c.document_id == self._document_id
                    )
                )
            ).scalar_one_or_none()

            await self._conn.execute(
                dmer_stage_run.update()
                .where(
                    dmer_stage_run.c.document_id == self._document_id,
                    dmer_stage_run.c.stage == _STAGE,
                    dmer_stage_run.c.status == _RUNNING,
                )
                .values(
                    status=_FAILED,
                    ended_at=now,
                    error_code="InterruptedAttempt",
                    error_detail="Interrupted attempt.",
                )
            )
            max_attempt = (
                await self._conn.execute(
                    select(func.max(dmer_stage_run.c.attempt_no)).where(
                        dmer_stage_run.c.document_id == self._document_id,
                        dmer_stage_run.c.stage == _STAGE,
                    )
                )
            ).scalar_one()
            attempt_no = (max_attempt or 0) + 1
            await self._conn.execute(
                _document.update()
                .where(_document.c.id == self._document_id)
                .values(attempt_count=_document.c.attempt_count + 1, updated_at=now)
            )
            run_id = (
                await self._conn.execute(
                    dmer_stage_run.insert()
                    .values(
                        document_id=self._document_id,
                        stage=_STAGE,
                        status=_RUNNING,
                        attempt_no=attempt_no,
                        started_at=now,
                    )
                    .returning(dmer_stage_run.c.id)
                )
            ).scalar_one()
            return ResolveDriverRun(
                run_id, attempt_no, driver_key, driver_licence, licence_read
            )

    async def resolve(
        self,
        run_id: int,
        *,
        mercury_driver: MercuryDriver,
        expected_document_count: int,
        resolved_by: str,
        licence_mismatch: bool,
        now: datetime,
    ) -> tuple[str, str]:
        """Attach the driver and its open evaluation; returns
        ``(driver_key, driver_evaluation_id)``."""
        self._check_session()
        licence = normalize_licence_number(mercury_driver.licence_number)
        async with self._conn.begin():
            await self._finish_stage(run_id, status=_SUCCEEDED, now=now)
            values = {
                "mercury_driver_id": mercury_driver.mercury_driver_id,
                "first_name": mercury_driver.first_name,
                "last_name": mercury_driver.last_name,
                "last_synced_at": now,
            }
            driver_key = (
                await self._conn.execute(
                    pg_insert(driver)
                    .values(licence_number=licence, **values)
                    .on_conflict_do_update(
                        index_elements=[driver.c.licence_number], set_=values
                    )
                    .returning(driver.c.driver_key)
                )
            ).scalar_one()
            attached = await self._conn.execute(
                _document.update()
                .where(
                    _document.c.id == self._document_id,
                    _document.c.pipeline_status == _READY,
                )
                .values(
                    driver_key=driver_key,
                    driver_resolved_by=resolved_by,
                    licence_mismatch=licence_mismatch,
                    updated_at=now,
                )
                .returning(_document.c.id)
            )
            if attached.first() is None:
                raise ResolveDriverStateError()
            evaluation = {
                "expected_document_count": expected_document_count,
                "last_mercury_check_at": now,
            }
            evaluation_id = (
                await self._conn.execute(
                    pg_insert(_driver_evaluation)
                    .values(
                        driver_key=driver_key, open=True, **evaluation
                    )  # status: WAITING
                    .on_conflict_do_update(
                        index_elements=["driver_key", "open"], set_=evaluation
                    )
                    .returning(_driver_evaluation.c.id)
                )
            ).scalar_one()
            return str(driver_key), str(evaluation_id)

    async def manual_review(self, run_id: int, *, reason: str, now: datetime) -> None:
        """Route the document to MANUAL_REVIEW with *reason* (a
        :data:`MANUAL_REVIEW_REASONS` code)."""
        if reason not in MANUAL_REVIEW_REASONS:
            raise ValueError(f"unknown manual review reason {reason!r}")
        self._check_session()
        async with self._conn.begin():
            await self._finish_stage(
                run_id,
                status=_FAILED,
                now=now,
                error_code=reason,
                error_detail=MANUAL_REVIEW_REASONS[reason],
            )
            routed = await self._conn.execute(
                _document.update()
                .where(
                    _document.c.id == self._document_id,
                    _document.c.pipeline_status == _READY,
                )
                .values(
                    pipeline_status="MANUAL_REVIEW",
                    manual_review_reason=reason,
                    updated_at=now,
                )
                .returning(_document.c.id)
            )
            if routed.first() is None:
                raise ResolveDriverStateError()

    async def fail(
        self, run_id: int, *, ended_at: datetime, error_code: str, error_detail: str
    ) -> None:
        """Mark a running attempt failed; never downgrades a committed result."""
        self._check_session()
        async with self._conn.begin():
            await self._conn.execute(
                dmer_stage_run.update()
                .where(
                    dmer_stage_run.c.id == run_id,
                    dmer_stage_run.c.document_id == self._document_id,
                    dmer_stage_run.c.stage == _STAGE,
                    dmer_stage_run.c.status == _RUNNING,
                )
                .values(
                    status=_FAILED,
                    ended_at=ended_at,
                    error_code=error_code,
                    error_detail=error_detail,
                )
            )

    async def _finish_stage(
        self,
        run_id: int,
        *,
        status: str,
        now: datetime,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        finished = await self._conn.execute(
            dmer_stage_run.update()
            .where(
                dmer_stage_run.c.id == run_id,
                dmer_stage_run.c.document_id == self._document_id,
                dmer_stage_run.c.stage == _STAGE,
                dmer_stage_run.c.status == _RUNNING,
            )
            .values(
                status=status,
                ended_at=now,
                error_code=error_code,
                error_detail=error_detail,
            )
            .returning(dmer_stage_run.c.id)
        )
        if finished.first() is None:
            raise ResolveDriverStateError()


@asynccontextmanager
async def resolve_driver_session(engine: AsyncEngine, document_id: str):
    """Yield a per-document Resolve Driver repository bound to one connection
    holding the document's advisory lock (contention: ``ResolveDriverBusyError``)."""
    async with advisory_lock_session(
        engine, f"resolve-driver:{document_id}", busy_error=ResolveDriverBusyError
    ) as conn:
        yield ResolveDriverRepository(conn, document_id=document_id)
