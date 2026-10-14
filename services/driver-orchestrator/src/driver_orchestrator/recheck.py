"""Self-scheduled re-check of a driver Mercury could not verify.

When the Decision Gateway can't verify a driver's batch with Mercury (the API
is down or timing out, or it returns no driver or several), the batch is
recorded WAITING -- it must never be decided on unverified data. Nothing else
would evaluate that driver again unless another of its DMERs signals, so the
trigger schedules a re-check: a ``driver-decision`` message for the same
driver, held by Service Bus until the backoff delay has passed. Delays grow
(15 min -> 4 h); the last re-check is final:
if Mercury still can't verify the batch, its undecided DMERs go to
MANUAL_REVIEW with a fallback IN outcome and a note saying why, rather than
waiting with nothing left to re-check them.

The envelope's ``attempt`` counts the chain: the original signal is 1, each
re-check one more.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from dmer_common.dto import (
    DRIVER_DECISION_EVENT,
    DriverDecisionMessage,
    event_message_id,
)

# Delay before re-check n (n = 1, 2, ...): about 7.75 h in total.
RECHECK_DELAYS = tuple(timedelta(minutes=m) for m in (15, 30, 60, 120, 240))


def recheck_delay(attempt: int) -> timedelta | None:
    """Delay before the re-check that follows a message with *attempt*, or
    None once the re-checks are used up."""
    index = attempt - 1  # attempt 1 (the original signal) -> first delay
    if 0 <= index < len(RECHECK_DELAYS):
        return RECHECK_DELAYS[index]
    return None


def recheck_message(
    message: DriverDecisionMessage, *, now: datetime
) -> DriverDecisionMessage:
    """The re-check that follows *message*: same driver and pointers, the next
    attempt, its own message_id (never the original signal's)."""
    attempt = message.attempt + 1
    return DriverDecisionMessage(
        message_id=event_message_id(
            DRIVER_DECISION_EVENT,
            f"{message.driver_key}:recheck:{attempt}:{message.message_id}",
        ),
        document_id=message.document_id,
        document_guid=message.document_guid,
        driver_key=message.driver_key,
        blob_url=message.blob_url,
        attempt=attempt,
        enqueued_at=now,
    )
