"""DB unit of work for the Decision Gateway (Driver Orchestration).

See docs/development/stages/06-driver-orchestration.md and
07-decision-gateway.md. Like the per-document activities (Normalize, Rule
Engine, Driver Lookup), every write goes through one connection holding an
advisory lock -- here per **driver**, so two signals for the same driver can
never evaluate its batch at the same time (the Service Bus session on
``driver-decision`` is the first guard; this is the second).

A driver's **batch** is the set of documents counted into its open
``driver_evaluation`` (the ``driver_evaluation_document`` markers the Rule
Engine writes). Driver Lookup opens the evaluation and sets
``expected_document_count`` from Mercury; the Rule Engine increments
``completed_document_count`` once per document.

The evaluation's status moves WAITING -> READY -> EVALUATING -> DECIDED
(-> POSTED, by Post-Processing), each move a compare-and-set validated by
:func:`validated_evaluation_status`. Deciding a batch, in one transaction:

- ``driver_evaluation``: status DECIDED, ``evaluated_at``, ``decision_summary``;
- one ``dmer_decision`` row per decided document (``decided_by = AI``), so
  Post-Processing reads the outcomes from the database -- they never travel
  in a message (decision_reason carries clinical content, section 9.2);
- each document: ``AWAITING_DRIVER_COMPLETION`` -> ``DECIDED`` (stage POST);
- each document's ``DECISION`` stage run: SUCCEEDED.

``decision_summary`` holds counts and ids only; per-document reasons carry
clinical content and are never written here or logged.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    and_,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ._advisory import advisory_lock_session
from .dmer_document import _pipeline_status_enum, _stage_enum
from .dmer_stage_run import dmer_stage_run
from .driver import driver

metadata = MetaData()


class EvaluationStatus(StrEnum):
    """``driver_evaluation.status`` (enum ``driver_evaluation_status``, V0001)."""

    WAITING = "WAITING"
    STALE = "STALE"
    READY = "READY"
    EVALUATING = "EVALUATING"
    DECIDED = "DECIDED"
    POSTED = "POSTED"


_E = EvaluationStatus
# Allowed moves. Back to WAITING from READY/EVALUATING: a fresh Mercury check
# found a new document, or the evaluation attempt failed and the sweeper will
# re-signal it. STALE is set by the Reconciliation Sweeper.
_TRANSITIONS: dict[EvaluationStatus, frozenset[EvaluationStatus]] = {
    _E.WAITING: frozenset({_E.READY, _E.STALE}),
    _E.STALE: frozenset({_E.READY, _E.WAITING}),
    _E.READY: frozenset({_E.EVALUATING, _E.WAITING}),
    _E.EVALUATING: frozenset({_E.DECIDED, _E.WAITING}),
    _E.DECIDED: frozenset({_E.POSTED}),
    _E.POSTED: frozenset(),
}

_evaluation_status_enum = PG_ENUM(
    *(s.value for s in EvaluationStatus),
    name="driver_evaluation_status",
    create_type=False,
)

# Mirrors of tables created by V0001 (driver_evaluation, dmer_extraction,
# rule_evaluation), V0004 (driver_evaluation_document) and V0005
# (dmer_document.driver_resolved_by) -- only the columns used here.
driver_evaluation = Table(
    "driver_evaluation",
    metadata,
    Column("id", PG_UUID(as_uuid=False), primary_key=True),
    Column("driver_key", PG_UUID(as_uuid=False), nullable=False),
    Column("status", _evaluation_status_enum, nullable=False),
    Column("open", Boolean, nullable=False),
    Column("expected_document_count", Integer),
    Column("completed_document_count", Integer, nullable=False),
    Column("last_mercury_check_at", DateTime(timezone=True)),
    Column("evaluated_at", DateTime(timezone=True)),
    Column("decision_summary", JSONB),
)
_counted = Table(
    "driver_evaluation_document",
    metadata,
    Column("driver_evaluation_id", PG_UUID(as_uuid=False), primary_key=True),
    Column("document_id", PG_UUID(as_uuid=False), primary_key=True),
)
_extraction = Table(
    "dmer_extraction",
    metadata,
    Column("document_id", PG_UUID(as_uuid=False), primary_key=True),
    Column("is_cutoff", Boolean),
    Column("page_count", Integer),
    Column("comparison_fields", JSONB),
    Column("comparison_hash", Text),
)
_rule_evaluation = Table(
    "rule_evaluation",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("document_id", PG_UUID(as_uuid=False), nullable=False),
    Column("rules_version", Text, nullable=False),
    Column("selected_outcome_code", Text),
    Column("selected_reason", Text),
    Column("priority_rank", Integer),
)
_document = Table(
    "dmer_document",
    metadata,
    Column("id", PG_UUID(as_uuid=False), primary_key=True),
    Column("document_guid", PG_UUID(as_uuid=False)),
    Column("received_date", DateTime(timezone=True)),
    Column("pipeline_status", _pipeline_status_enum),
    Column("current_stage", _stage_enum),
    Column("updated_at", DateTime(timezone=True)),
    Column("driver_resolved_by", Text),
)

_outcome_enum = PG_ENUM(
    "CP", "IN", "PR", "PU", "TCM", "CR", name="dmer_outcome_code", create_type=False
)
_decided_by_enum = PG_ENUM(
    "AI", "FALLBACK", "MANUAL", name="dmer_decided_by", create_type=False
)
dmer_decision = Table(
    "dmer_decision",
    metadata,
    Column(
        "id",
        PG_UUID(as_uuid=False),
        primary_key=True,
        server_default="gen_random_uuid()",
    ),
    Column("document_id", PG_UUID(as_uuid=False), nullable=False),
    Column("driver_evaluation_id", PG_UUID(as_uuid=False), nullable=False),
    Column("outcome_code", _outcome_enum, nullable=False),
    Column("is_duplicate", Boolean, nullable=False),
    Column("duplicate_of_document_id", PG_UUID(as_uuid=False)),
    Column("duplicate_reason", Text),
    Column("superseded_by_cutoff_rule", Boolean, nullable=False),
    Column("driver_mapped", Boolean, nullable=False),
    Column("proposed_driver_key", PG_UUID(as_uuid=False)),
    Column("decision_reason", JSONB),
    Column("decided_by", _decided_by_enum, nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=False),
)

_DECISION_STAGE = "DECISION"
_POST_STAGE = "POST"
_AWAITING = "AWAITING_DRIVER_COMPLETION"
_DECIDED_DOC = "DECIDED"
_RUNNING, _SUCCEEDED, _FAILED = "RUNNING", "SUCCEEDED", "FAILED"


class DriverDecisionBusyError(RuntimeError):
    """Another worker holds this driver's lock; retry later."""

    def __init__(self) -> None:
        super().__init__("Driver decision already in progress for this driver")


class DriverDecisionStateError(RuntimeError):
    """The evaluation or a document is not in the state this step requires --
    another worker moved it on, or the data is inconsistent."""


class InvalidEvaluationTransition(ValueError):
    """A ``driver_evaluation.status`` move the state machine does not allow."""


def validated_evaluation_status(
    current: EvaluationStatus, target: EvaluationStatus
) -> EvaluationStatus:
    """Return *target* if ``current -> target`` is allowed (same status is a no-op)."""
    if current == target or target in _TRANSITIONS[current]:
        return target
    raise InvalidEvaluationTransition(f"illegal transition {current} -> {target}")


@dataclass(frozen=True)
class DriverEvaluationState:
    """The driver's open evaluation."""

    id: str
    status: EvaluationStatus
    expected_document_count: int | None
    completed_document_count: int
    decision_summary: dict[str, Any] | None


@dataclass(frozen=True)
class DecisionRecord:
    """One document's decided outcome -- a ``dmer_decision`` row."""

    document_id: str
    outcome_code: str
    is_duplicate: bool = False
    duplicate_of_document_id: str | None = None
    duplicate_reason: str | None = None
    superseded_by_cutoff_rule: bool = False
    driver_mapped: bool = False
    proposed_driver_key: str | None = None
    decision_reason: dict[str, Any] | None = None  # clinical: never log


@dataclass(frozen=True)
class BatchDocument:
    """One document of the batch, with what the gateway decides on."""

    document_id: str
    document_guid: str
    received_date: datetime | None
    pipeline_status: str
    driver_resolved_by: str | None
    # dmer_extraction (None when extraction wrote no row)
    is_cutoff: bool | None
    page_count: int | None
    comparison_hash: str | None
    comparison_fields: dict[str, Any] | None
    # latest rule_evaluation (None when there is none) -- reason is clinical
    outcome_code: str | None
    priority_rank: int | None
    outcome_reason: str | None
    rules_version: str | None


class DriverDecisionRepository:
    """Decision Gateway DB operations bound to one dedicated async connection."""

    def __init__(self, conn: AsyncConnection, *, driver_key: str) -> None:
        self._conn = conn
        self._driver_key = driver_key

    def _check_session(self) -> None:
        # A transparently reconnected connection would not own our advisory lock.
        if self._conn.closed or self._conn.invalidated:
            raise DriverDecisionBusyError()

    async def open_evaluation(self) -> DriverEvaluationState | None:
        """The driver's open evaluation, or None if Driver Lookup made none."""
        self._check_session()
        async with self._conn.begin():
            row = (
                await self._conn.execute(
                    select(
                        driver_evaluation.c.id,
                        driver_evaluation.c.status,
                        driver_evaluation.c.expected_document_count,
                        driver_evaluation.c.completed_document_count,
                        driver_evaluation.c.decision_summary,
                    ).where(
                        driver_evaluation.c.driver_key == self._driver_key,
                        driver_evaluation.c.open.is_(True),
                    )
                )
            ).first()
        if row is None:
            return None
        return DriverEvaluationState(
            id=str(row.id),
            status=EvaluationStatus(row.status),
            expected_document_count=row.expected_document_count,
            completed_document_count=row.completed_document_count,
            decision_summary=row.decision_summary,
        )

    async def driver_licence(self) -> str | None:
        """The driver's canonical licence, for Mercury's ``GET by
        driver_licence``. PII: never log or return it further than the call."""
        self._check_session()
        async with self._conn.begin():
            return (
                await self._conn.execute(
                    select(driver.c.licence_number).where(
                        driver.c.driver_key == self._driver_key
                    )
                )
            ).scalar_one_or_none()

    async def load_batch(self, evaluation_id: str) -> list[BatchDocument]:
        """Every document counted into *evaluation_id*, oldest first, joined
        with its extraction values and its latest rule evaluation."""
        self._check_session()
        latest_rule = (
            select(
                _rule_evaluation.c.document_id,
                _rule_evaluation.c.selected_outcome_code,
                _rule_evaluation.c.selected_reason,
                _rule_evaluation.c.priority_rank,
                _rule_evaluation.c.rules_version,
            )
            .distinct(_rule_evaluation.c.document_id)
            .order_by(_rule_evaluation.c.document_id, _rule_evaluation.c.id.desc())
            .subquery("latest_rule")
        )
        doc = _document
        query = (
            select(
                doc.c.id,
                doc.c.document_guid,
                doc.c.received_date,
                doc.c.pipeline_status,
                doc.c.driver_resolved_by,
                _extraction.c.is_cutoff,
                _extraction.c.page_count,
                _extraction.c.comparison_hash,
                _extraction.c.comparison_fields,
                latest_rule.c.selected_outcome_code,
                latest_rule.c.priority_rank,
                latest_rule.c.selected_reason,
                latest_rule.c.rules_version,
            )
            .select_from(
                _counted.join(doc, doc.c.id == _counted.c.document_id)
                .outerjoin(_extraction, _extraction.c.document_id == doc.c.id)
                .outerjoin(latest_rule, latest_rule.c.document_id == doc.c.id)
            )
            .where(_counted.c.driver_evaluation_id == evaluation_id)
            .order_by(doc.c.received_date.asc().nulls_first(), doc.c.document_guid)
        )
        async with self._conn.begin():
            rows = (await self._conn.execute(query)).all()
        return [
            BatchDocument(
                document_id=str(r.id),
                document_guid=str(r.document_guid),
                received_date=r.received_date,
                pipeline_status=str(r.pipeline_status),
                driver_resolved_by=r.driver_resolved_by,
                is_cutoff=r.is_cutoff,
                page_count=r.page_count,
                comparison_hash=(r.comparison_hash or "").strip() or None,
                comparison_fields=r.comparison_fields,
                outcome_code=r.selected_outcome_code,
                priority_rank=r.priority_rank,
                outcome_reason=r.selected_reason,
                rules_version=r.rules_version,
            )
            for r in rows
        ]

    async def move_evaluation(
        self,
        evaluation_id: str,
        *,
        expected: EvaluationStatus,
        target: EvaluationStatus,
        now: datetime,
        expected_document_count: int | None = None,
        mercury_checked: bool = False,
    ) -> None:
        """Compare-and-set ``status`` from *expected* to *target*.

        ``mercury_checked`` records ``last_mercury_check_at = now`` (and, when
        given, the refreshed ``expected_document_count``). Raises
        :class:`DriverDecisionStateError` if the row is no longer in *expected*.
        """
        validated_evaluation_status(expected, target)
        self._check_session()
        values: dict[str, Any] = {"status": target.value}
        if mercury_checked:
            values["last_mercury_check_at"] = now
        if expected_document_count is not None:
            values["expected_document_count"] = expected_document_count
        async with self._conn.begin():
            moved = await self._conn.execute(
                driver_evaluation.update()
                .where(
                    driver_evaluation.c.id == evaluation_id,
                    driver_evaluation.c.open.is_(True),
                    driver_evaluation.c.status == expected.value,
                )
                .values(**values)
                .returning(driver_evaluation.c.id)
            )
            if moved.first() is None:
                raise DriverDecisionStateError()

    async def start_decision(
        self, evaluation_id: str, document_ids: list[str], *, now: datetime
    ) -> dict[str, int]:
        """READY -> EVALUATING, with a RUNNING ``DECISION`` stage run per document.

        Also re-entered from EVALUATING (a crashed or retried attempt): its
        leftover RUNNING runs are closed as interrupted first. Returns
        ``{document_id: run_id}``.
        """
        self._check_session()
        async with self._conn.begin():
            status = (
                await self._conn.execute(
                    select(driver_evaluation.c.status)
                    .where(
                        driver_evaluation.c.id == evaluation_id,
                        driver_evaluation.c.open.is_(True),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if status not in (_E.READY.value, _E.EVALUATING.value):
                raise DriverDecisionStateError()
            await self._conn.execute(
                driver_evaluation.update()
                .where(driver_evaluation.c.id == evaluation_id)
                .values(status=_E.EVALUATING.value)
            )
            await self._conn.execute(
                dmer_stage_run.update()
                .where(
                    dmer_stage_run.c.document_id.in_(document_ids),
                    dmer_stage_run.c.stage == _DECISION_STAGE,
                    dmer_stage_run.c.status == _RUNNING,
                )
                .values(
                    status=_FAILED,
                    ended_at=now,
                    error_code="InterruptedAttempt",
                    error_detail="Interrupted attempt.",
                )
            )
            attempts = dict(
                (
                    await self._conn.execute(
                        select(
                            dmer_stage_run.c.document_id,
                            func.max(dmer_stage_run.c.attempt_no),
                        )
                        .where(
                            dmer_stage_run.c.document_id.in_(document_ids),
                            dmer_stage_run.c.stage == _DECISION_STAGE,
                        )
                        .group_by(dmer_stage_run.c.document_id)
                    )
                ).all()
            )
            runs: dict[str, int] = {}
            for document_id in document_ids:
                runs[document_id] = (
                    await self._conn.execute(
                        dmer_stage_run.insert()
                        .values(
                            document_id=document_id,
                            stage=_DECISION_STAGE,
                            status=_RUNNING,
                            attempt_no=(attempts.get(document_id) or 0) + 1,
                            started_at=now,
                        )
                        .returning(dmer_stage_run.c.id)
                    )
                ).scalar_one()
            return runs

    async def record_decided(
        self,
        evaluation_id: str,
        runs: dict[str, int],
        *,
        summary: dict[str, Any],
        now: datetime,
        decisions: tuple[DecisionRecord, ...] = (),
    ) -> None:
        """EVALUATING -> DECIDED, every document -> DECIDED, runs SUCCEEDED,
        and one ``dmer_decision`` row per entry in *decisions*.

        One transaction. Raises :class:`DriverDecisionStateError` (and changes
        nothing) if the evaluation is no longer EVALUATING, any document is no
        longer awaiting its driver, or a decision is for a document outside
        *runs*.
        """
        if not {d.document_id for d in decisions} <= set(runs):
            raise DriverDecisionStateError()
        self._check_session()
        async with self._conn.begin():
            moved = await self._conn.execute(
                driver_evaluation.update()
                .where(
                    driver_evaluation.c.id == evaluation_id,
                    driver_evaluation.c.open.is_(True),
                    driver_evaluation.c.status == _E.EVALUATING.value,
                )
                .values(
                    status=_E.DECIDED.value,
                    evaluated_at=now,
                    decision_summary=summary,
                )
                .returning(driver_evaluation.c.id)
            )
            if moved.first() is None:
                raise DriverDecisionStateError()
            decided = await self._conn.execute(
                _document.update()
                .where(
                    _document.c.id.in_(list(runs)),
                    _document.c.pipeline_status == _AWAITING,
                )
                .values(
                    pipeline_status=_DECIDED_DOC,
                    current_stage=_POST_STAGE,
                    updated_at=now,
                )
                .returning(_document.c.id)
            )
            if len(decided.all()) != len(runs):
                raise DriverDecisionStateError()  # rolls the transaction back
            if decisions:
                await self._conn.execute(
                    dmer_decision.insert(),
                    [
                        {
                            "document_id": d.document_id,
                            "driver_evaluation_id": evaluation_id,
                            "outcome_code": d.outcome_code,
                            "is_duplicate": d.is_duplicate,
                            "duplicate_of_document_id": d.duplicate_of_document_id,
                            "duplicate_reason": d.duplicate_reason,
                            "superseded_by_cutoff_rule": d.superseded_by_cutoff_rule,
                            "driver_mapped": d.driver_mapped,
                            "proposed_driver_key": d.proposed_driver_key,
                            "decision_reason": d.decision_reason,
                            "decided_by": "AI",
                            "decided_at": now,
                        }
                        for d in decisions
                    ],
                )
            await self._finish_runs(runs, status=_SUCCEEDED, now=now)

    async def fail_decision(
        self,
        evaluation_id: str,
        runs: dict[str, int],
        *,
        error_code: str,
        now: datetime,
    ) -> None:
        """EVALUATING -> WAITING (the sweeper re-signals it), runs FAILED."""
        self._check_session()
        async with self._conn.begin():
            await self._conn.execute(
                driver_evaluation.update()
                .where(
                    driver_evaluation.c.id == evaluation_id,
                    driver_evaluation.c.status == _E.EVALUATING.value,
                )
                .values(status=_E.WAITING.value)
            )
            await self._finish_runs(
                runs,
                status=_FAILED,
                now=now,
                error_code=error_code,
                error_detail="Decision attempt failed; see error_code.",
            )

    async def _finish_runs(
        self,
        runs: dict[str, int],
        *,
        status: str,
        now: datetime,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        if not runs:
            return
        await self._conn.execute(
            dmer_stage_run.update()
            .where(
                and_(
                    dmer_stage_run.c.id.in_(list(runs.values())),
                    dmer_stage_run.c.status == _RUNNING,
                )
            )
            .values(
                status=status,
                ended_at=now,
                error_code=error_code,
                error_detail=error_detail,
            )
        )


@asynccontextmanager
async def driver_decision_session(engine: AsyncEngine, driver_key: str):
    """Yield a :class:`DriverDecisionRepository` on a dedicated connection
    holding the driver's advisory lock (contention: ``DriverDecisionBusyError``)."""
    async with advisory_lock_session(
        engine, f"driver-decision:{driver_key}", busy_error=DriverDecisionBusyError
    ) as conn:
        yield DriverDecisionRepository(conn, driver_key=driver_key)
