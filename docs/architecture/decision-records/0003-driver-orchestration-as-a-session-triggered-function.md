# ADR-0003: Driver Orchestration runs as a session-triggered function, not a Durable orchestration

Status: Accepted (approved by the architect)
Date: 2026-10-14

## Context

Stage 4 of the revised architecture (architecture document §4.4,
[06-driver-orchestration.md](../../development/stages/06-driver-orchestration.md)) is the
per-driver join: once all of a driver's waiting DMERs are processed, the Decision Gateway decides
them together, one driver at a time. The architecture describes it as a Durable Functions
orchestration per driver (`instanceId = driver_key`), started by a trigger on the session-enabled
`driver-decision` queue, relying on two protections: the Service Bus session (one message per
driver at a time) and the instance id (one orchestration per driver at a time).

Every document's Signal Driver step publishes a `driver-decision` message when the document is
ready, so a driver's batch is re-evaluated each time one of its DMERs finishes. A Durable starter,
however, holds the session lock only while it *starts* the orchestration; the orchestration itself
runs outside the lock. So:

- a second signal for the same driver can arrive while that driver's orchestration is still
  running. The starter cannot start a second instance with the same id; if it ignores the message
  (as the per-document starter does, correctly, for documents) the signal is lost;
- a signal can arrive after an orchestration ended WAITING; treating "already completed" as
  "nothing to do" again loses it.

If the run that saw the batch incomplete was the last one, the driver is never evaluated again and
its DMERs never reach Intake -- the "lost update" the architecture document says the join must
prevent. Making the Durable design safe needs extra machinery: restarting completed instances,
raising a "re-check" event to running ones and looping before completing, plus the Reconciliation
Sweeper to catch the remaining timing gap.

## Decision

The Driver Orchestration is a **Service Bus–triggered function with sessions enabled**
(`DriverDecision` in `services/driver-orchestrator`, its own Flex Consumption Function App). The
function evaluates the driver's batch itself, inside the session lock:

- the Functions host processes one driver's messages one at a time, **each to completion**, so a
  signal that arrives during an evaluation waits its turn instead of being dropped; different
  drivers run in parallel;
- a per-driver Postgres advisory lock is a second guard (`dmer_common.db.driver_decision`);
- failures are retried by Service Bus redelivery (dead-lettered after the queue's max delivery
  count); an unverifiable batch schedules its own re-check with backoff and, on the last one, falls
  back to manual review;
- the decided outcomes are written to `dmer_decision` in the same transaction as the batch's
  DECIDED state, for Post-Processing to read.

All of the logic is in one function (`evaluate.evaluate_driver`); only the trigger is specific to
this decision, so a Durable design could reuse it unchanged as an activity.

## Consequences

- Lost or ignored signals cannot happen by construction; no restart / re-check-event machinery or
  orchestration replay rules are needed, and the code is ordinary, directly testable functions.
- No Durable task hub state: the app's storage account needs no queue/table state, and there are
  no in-flight orchestrations to protect during a deploy.
- The Decision Gateway and Post-Processing are not chained as activities of one orchestration.
  The hand-off is through the database (`dmer_decision`), and Post-Processing is started
  separately (to be agreed with the Post-Processing epic).
- An evaluation must finish within the session lock duration (default 5 minutes). It is a few
  queries plus one Mercury lookup, so seconds.
- This departs from the architecture document's §4.4 wording, which should be read with this ADR;
  [06-driver-orchestration.md](../../development/stages/06-driver-orchestration.md) points here.
