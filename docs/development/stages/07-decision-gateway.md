# Activity: Decision Gateway

Source: architecture doc §4.4.3. Part of [Driver Orchestration](06-driver-orchestration.md).
Data model: `../data-model.md`.

## Purpose

Determines whether a driver's DMER batch is complete and, if so, computes the outcome for every
document in it: completeness, cut-off precedence, duplicate detection, differing-content handling,
single-document pass-through, and driver-mapping proposals. **Does not write `dmer_decision`** —
it computes the outcome; [Post-Processing](08-post-processing.md) commits it together with the
outbox rows so the decision and the intent to deliver it cannot be separated.

## Trigger / reads / writes

| | |
|---|---|
| Type | Durable activity inside the Driver Orchestrator |
| Reads | Mercury `GET by driver_licence`; `dmer_document` and `dmer_extraction` for every document of the driver; `rule_evaluation` for each |
| Writes | `driver_evaluation` only — nothing else until the decision is final |
| Runs | Once per driver batch, never concurrently for the same driver (see `../06-driver-orchestration.md`) |

## Step 1 — Completeness

Call Mercury `GET by driver_licence`. Take the documents whose `mercury_document_status` is
`Uploaded` **and** whose `dps_date` is empty (question I-9: an empty `dps_date` is a reliable
"not yet triaged" signal). Per question I-8 (answered): a document with status `Rejected` **does
count** toward "all documents processed" — do not wait on it.

If any qualifying document has not reached `RULES_APPLIED` in our database, set
`driver_evaluation.status = WAITING`, record `last_mercury_check_at`, and **stop**. This is an
explicit recorded state, not a silent return — the entire point of this stage's redesign.

## Step 2 — Cut-off precedence

| Situation | Outcome |
|---|---|
| All DMERs for the driver are cut off | `IN` — send to Intake for review, with the cut-off reason surfaced so Intake can request a re-fax rather than re-reading an illegible page (question I-5, answered). |
| Some cut off, at least one clear | Evaluate using the clear scan only; the cut-off documents are excluded from the outcome decision and marked `superseded_by_cutoff_rule`. |
| None cut off | Proceed to Step 3 with all documents. |

**Recency never wins over legibility** (question I-4, answered): if a driver has one cut-off and
one clear DMER and the cut-off one is newer, still use the clear one for the outcome — *unless*
their content actually differs, in which case send the case for manual review (`IN`) rather than
silently preferring either. Concretely: compare the cut-off document's readable fields against the
clear document's; if they agree, use the clear document; if they disagree (or disagreement can't be
established because the cut-off document doesn't have enough surviving content to compare), route
to manual review.

## Step 3 — Duplicate detection

Compare the canonical `comparison_hash` values from `dmer_extraction`. **Identical hash means
duplicate.** The oldest document by `uploaded_date` (tie-broken by `received_date`, then
`document_guid`) keeps the rule engine outcome; the others are marked `is_duplicate` with
`duplicate_of_document_id` pointing at the retained one.

**Do not use full JSON equality for duplicate detection.** Two scans of the same physical page will
differ in whitespace, confidence values, and occasional characters — e.g. on the sample DMER,
section D contains handwritten scores such as "MMSE 26/30" and "MoCA 19/30", and two OCR passes can
easily disagree on a digit. This is exactly why `dmer_extraction.comparison_fields` is an explicit,
canonicalized (trimmed, lowercased, normalized dates/numbers) field set, hashed and compared as a
hash — not a blob diff. The failure direction is safe: a **false negative** (two identical
documents judged as differing) routes them to a human, which is the correct way for this check to
fail. A false positive (two genuinely different documents judged identical) is the failure to avoid
— keep the comparison field set conservative.

## Step 4 — Differing content

If two non-duplicate, non-cut-off DMERs exist for the driver and their `comparison_fields` differ,
the outcome is `IN` for the batch. Store the field-level diff in `decision_reason` so Intake can see
*what* differed rather than being told only that something did.

Per question I-6 (answered, pending final Intake confirmation): **process the newest DMER; mark the
older one as duplicate/rejected.** This is the working rule for how many decision rows/outbox
operations get produced when two DMERs differ — one `IN` (or the rule-engine-derived outcome) for
the newest, one `MARK_DUPLICATE` for the older. Confirm with Intake before build-freeze (see
[Open Questions](#open-questions--decisions-required) — the architecture document itself flags this
answer as needing final confirmation).

## Step 5 — Single document

If exactly one qualifying DMER exists, keep the rule engine outcome unchanged.

## Step 6 — Driver mapping

If Mercury returned no driver object but a licence was read from the page and resolves to exactly
one driver, record `driver_mapped = false` and `proposed_driver_key` so Post-Processing can request
the mapping. Per question I-11 (answered): **the AI may map the driver automatically** using the
Mercury API — this is not a human-confirm-every-mapping requirement. When the licence matches no
driver or matches more than one (question I-12, answered): send for human review, mentioning the
licence information and reason in the comment.

## Database writes

| Table | Operation | Fields |
|---|---|---|
| `driver_evaluation` | `UPDATE` | `status` moves `WAITING` → `READY` → `EVALUATING` → `DECIDED`; `expected_document_count` refreshed from the live Mercury response; `completed_document_count`; `last_mercury_check_at`; `evaluated_at`; `decision_summary` (jsonb — how many documents, how many duplicates, how many cut off, which document was retained, which rule path was taken). |
| `dmer_document` | `UPDATE` | `mercury_document_status` and `dps_date` refreshed from the completeness call — **this is the safe point** to refresh Mercury-side metadata (see `01-ingest.md#why-do-nothing-not-do_update`); `pipeline_status = DECIDED` once step 6 completes. |
| `dmer_stage_run` | `INSERT` then `UPDATE` | `stage = DECISION`, `status`, `attempt_no`, timings. |

`dmer_decision` rows are **not** written here — see [Post-Processing](08-post-processing.md).

## Failure handling

A Mercury `GET by driver_licence` failure at Step 1 is transient — retry with backoff; if the
retry budget is exhausted, leave `driver_evaluation.status = WAITING` rather than guessing at
completeness, and let the [Reconciliation Sweeper](09-reliability-components.md) retry the whole
gateway pass later. Never proceed to Steps 2–6 on stale or partial completeness data.

## Interaction with upstream/downstream

Reads the outputs of every sibling document's [Rule Engine activity](05-activity-rule-engine.md).
Feeds [Post-Processing](08-post-processing.md) within the same orchestration instance.

## Configuration / environment variables

Shares `MERCURY_DRIVER_LICENCE_API_BASE_URL` and Mercury credentials with
[Driver Orchestration](06-driver-orchestration.md).

## Idempotency requirements

Safe to re-run: it only ever reads plus updates `driver_evaluation`/`dmer_document` metadata fields
that are themselves idempotent (re-setting the same completeness state twice is harmless). The
serialization guarantee from [Driver Orchestration](06-driver-orchestration.md) is what prevents
two concurrent runs, not this activity's own logic.

## Logging / auditing

Log the full `decision_summary` structure at the point it's computed (into the DB row) — but keep
INFO-level logs to counts and outcome codes, not field-level diffs (those can carry
near-clinical content; see §9.2).

## Implementation considerations for Claude Code

- This activity has no placeholder code anywhere in the repo — see
  `06-driver-orchestration.md#alignment-gaps-vs-current-code` for the recommended service folder.
- `libs/dmer_common/src/dmer_common/mercury_client/__init__.py` needs the `GET by driver_licence`
  call added (currently an empty docstring stub) — question M-5 confirms this endpoint returns only
  DMERs, which simplifies the completeness filter (no need to exclude non-DMER document types).
- The comparison-hash lookup (`dmer_extraction.comparison_hash`, indexed per `../data-model.md`)
  should be a simple equi-join across the driver's documents — no fuzzy matching needed given the
  canonicalization happens upstream in Extraction.

## Example payload — `decision_summary` (illustrative shape, not a fixed schema)

```json
{
  "document_count": 3,
  "duplicate_count": 1,
  "cut_off_count": 1,
  "retained_document_id": "8f3c1b2a-...",
  "rule_path": "PR",
  "notes": "one cut-off document superseded by a clear sibling; one duplicate by comparison_hash"
}
```

## Open Questions / Decisions Required

- **I-6** — confirmed direction (process newest, mark older duplicate/rejected) but the
  architecture document itself notes this needs final Intake confirmation before build-freeze.
- **I-4** cut-off-vs-clear content disagreement handling (route to manual review) is this
  document's interpretation of the answer given; confirm the exact business framing with Intake
  before implementing Step 2's disagreement branch.
- Whether Step 4's "two non-duplicate, non-cut-off DMERs differ" logic generalizes correctly to
  **three or more** qualifying documents (which pair(s) get compared, and what happens if some
  pairs agree and others don't) is not addressed in the architecture document — clarify before
  Phase 4 build if drivers with 3+ DMERs are expected to be common.
