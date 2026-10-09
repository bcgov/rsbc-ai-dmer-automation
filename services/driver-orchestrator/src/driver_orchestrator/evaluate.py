"""Evaluate one driver's batch: the whole Decision Gateway for one signal.

Called once per ``driver-decision`` signal, one at a time per driver (the
Service Bus session), under the driver's advisory lock (the second guard):

1. Find the driver's open evaluation (Driver Lookup made it).
2. Load the batch: every document counted into it.
3. Re-check Mercury (``GET by driver_licence``) -- "verify twice".
4. Completeness. Not ready -> record WAITING (with the Mercury check time
   and refreshed count) and stop; the next document's signal, or the
   Reconciliation Sweeper, evaluates it again.
5. Ready -> READY -> EVALUATING (a ``DECISION`` stage run per document),
   ``decide()``, then DECIDED + one ``dmer_decision`` row per document in one
   transaction. A failure after EVALUATING goes back to WAITING and is
   re-raised so the signal is retried.

Design-neutral: the session-triggered function calls this directly; a
Durable design would call it as an activity. Logs counts, statuses and ids
only -- never the licence, the rule reasons or the comparison fields.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from dmer_common.db.driver_decision import (
    DecisionRecord,
    EvaluationStatus,
    driver_decision_session,
)
from dmer_common.telemetry import get_logger
from sqlalchemy.ext.asyncio import AsyncEngine

from .batch import to_facts
from .decision import (
    DocumentDecision,
    WaitReason,
    check_completeness,
    decide,
    mercury_waiting_count,
)

_log = get_logger(__name__)
_E = EvaluationStatus

# Statuses that can still be evaluated.
_RE_EVALUABLE = frozenset({_E.WAITING, _E.STALE, _E.READY, _E.EVALUATING})


class Result(StrEnum):
    """What one evaluation did (returned, and logged)."""

    DECIDED = "decided"
    WAITING = "waiting"
    NO_OPEN_EVALUATION = "no_open_evaluation"
    ALREADY_DECIDED = "already_decided"


@dataclass(frozen=True)
class Evaluation:
    """The outcome of :func:`evaluate_driver` (counts and ids only)."""

    result: Result
    evaluation_id: str | None = None
    wait_reason: WaitReason | None = None
    expected: int | None = None
    done: int = 0
    decided_count: int = 0


# Mercury GET by driver_licence: licence -> list of driver records ([] = none).
MercuryLookup = Callable[[str], list[dict[str, Any]]]


def decision_record(decision: DocumentDecision) -> DecisionRecord:
    """A gateway decision as a ``dmer_decision`` row."""
    duplicate_reason = None
    if decision.is_duplicate:
        duplicate_reason = decision.path.value
    return DecisionRecord(
        document_id=decision.document_id,
        outcome_code=decision.outcome_code,
        is_duplicate=decision.is_duplicate,
        duplicate_of_document_id=decision.duplicate_of_document_id,
        duplicate_reason=duplicate_reason,
        superseded_by_cutoff_rule=decision.superseded_by_cutoff_rule,
        driver_mapped=decision.driver_mapped,
        proposed_driver_key=decision.proposed_driver_key,
        decision_reason=decision.reason,
    )


async def evaluate_driver(
    engine: AsyncEngine,
    driver_key: str,
    *,
    mercury_lookup: MercuryLookup,
    counted_types: frozenset[str],
    uncounted_statuses: frozenset[str],
    session=driver_decision_session,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Evaluation:
    """Run the Decision Gateway for *driver_key* once (see the module docstring).

    ``session`` is injectable for unit tests. Raises ``DriverDecisionBusyError``
    if another worker holds the driver's lock (retry later).
    """
    async with session(engine, driver_key) as repo:
        evaluation = await repo.open_evaluation()
        if evaluation is None:
            _log.warning("decision gateway: driver has no open evaluation")
            return Evaluation(Result.NO_OPEN_EVALUATION)
        if evaluation.status not in _RE_EVALUABLE:
            # DECIDED/POSTED: a late signal for a batch already decided.
            _log.info(
                "decision gateway: batch already decided",
                extra={"driver_evaluation_id": evaluation.id},
            )
            return Evaluation(Result.ALREADY_DECIDED, evaluation.id)

        batch = await repo.load_batch(evaluation.id)
        expected = await _mercury_expected(
            repo, mercury_lookup, counted_types, uncounted_statuses
        )
        completeness = check_completeness(expected, batch)
        now = clock()

        if not completeness.ready:
            await repo.move_evaluation(
                evaluation.id,
                expected=evaluation.status,
                target=_E.WAITING,
                now=now,
                expected_document_count=expected,
                mercury_checked=True,
            )
            _log.info(
                "decision gateway: waiting",
                extra={
                    "driver_evaluation_id": evaluation.id,
                    "wait_reason": completeness.wait_reason,
                    "expected": expected,
                    "done": completeness.done,
                    "batch_size": len(batch),
                },
            )
            return Evaluation(
                Result.WAITING,
                evaluation.id,
                completeness.wait_reason,
                expected,
                completeness.done,
            )

        if evaluation.status in (_E.WAITING, _E.STALE):
            await repo.move_evaluation(
                evaluation.id,
                expected=evaluation.status,
                target=_E.READY,
                now=now,
                expected_document_count=expected,
                mercury_checked=True,
            )
        to_decide = [d for d in batch if d.document_id in set(completeness.to_decide)]
        runs = await repo.start_decision(
            evaluation.id, [d.document_id for d in to_decide], now=now
        )
        try:
            records: tuple[DecisionRecord, ...] = ()
            summary: dict[str, Any] = {"document_count": 0}
            if to_decide:
                batch_decision = decide(
                    [to_facts(d, driver_key=driver_key) for d in to_decide]
                )
                records = tuple(decision_record(d) for d in batch_decision.decisions)
                summary = batch_decision.summary
            summary = {
                **summary,
                "expected_document_count": expected,
                "finished_document_count": completeness.done,
                "not_decided_document_count": len(batch) - len(to_decide),
            }
            await repo.record_decided(
                evaluation.id, runs, summary=summary, now=clock(), decisions=records
            )
        except Exception as exc:
            try:
                await repo.fail_decision(
                    evaluation.id, runs, error_code=type(exc).__name__, now=clock()
                )
            except Exception:  # noqa: BLE001 - keep the original failure
                _log.error("decision gateway: unable to record failed attempt")
            raise

        _log.info(
            "decision gateway: decided",
            extra={
                "driver_evaluation_id": evaluation.id,
                "decided_count": len(records),
                "outcome_counts": summary.get("outcome_counts"),
                "rejected_count": summary.get("rejected_count"),
            },
        )
        return Evaluation(
            Result.DECIDED,
            evaluation.id,
            None,
            expected,
            completeness.done,
            len(records),
        )


async def _mercury_expected(
    repo,
    mercury_lookup: MercuryLookup,
    counted_types: frozenset[str],
    uncounted_statuses: frozenset[str],
) -> int | None:
    """Mercury's waiting DMERs for the driver, or None if it can't be verified
    (no licence on record, Mercury unavailable, no driver or several)."""
    licence = await repo.driver_licence()
    if not licence:
        _log.error("decision gateway: driver has no licence on record")
        return None
    try:
        drivers = await asyncio.to_thread(mercury_lookup, licence)
    except Exception as exc:  # noqa: BLE001 - wait and retry later, never guess
        _log.error(
            "decision gateway: Mercury lookup failed",
            extra={"error_code": type(exc).__name__},
        )
        return None
    return mercury_waiting_count(
        drivers, counted_types=counted_types, uncounted_statuses=uncounted_statuses
    )
