"""What the triage LLM is sent: the normalized DMER without identifiers, and
the checklist of conditions it must evaluate.

The normalized DMER keeps the form's identity fields; :func:`redact` removes
them (and their evidence) and replaces the date of birth with the age at the
exam date, which the procedures' age rules need. Free text is sent as is.
"""

from __future__ import annotations

import json
from datetime import date

# Identifying fields never sent to the model: the procedures don't need them.
REDACTED_FIELDS = (
    "first_name",
    "last_name",
    "dl_number",
    "date_of_birth",  # replaced by age_at_exam
    "physician_or_np_fax_present",  # the physician's contact block (address, phone, fax)
    "scan_from_information",  # the fax header of the scanned form
    "relationship_with_patient.name_and_address",
    "doctor_signature",
)

# Normalization sets this on every DMER with no diabetes-treatment box ticked,
# diabetic or not; the rules read it only together with diabetes. Sent alone,
# the model applied the diabetes rule to non-diabetic DMERs (POC: DMER4, 8, 9).
_DIABETES_TREATMENT_FLAG = "endocrine.diabetes_treatment_not_indicated"

# True boolean fields that are form or derived bookkeeping, not conditions.
_NOT_CONDITIONS = (
    "opinion.",
    "visual_acuity.",
    "visual_field.",
    "driver_consent.",
    "relationship_with_patient.",
)
_CONCERN_SUFFIXES = ("_has_concerns", "_has_concern", ".has_concerns")

_MIN_AGE, _MAX_AGE = 14, 110


def fields_of(normalized: dict) -> dict:
    """The field -> value map of a normalized DMER (bare, or ``{"dmer": ...}``)."""
    inner = normalized.get("dmer")
    return inner if isinstance(inner, dict) else normalized


def _iso(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def age_at_exam(
    date_of_birth: object, exam_date: object
) -> tuple[int | None, str | None]:
    """Age in whole years on the exam date, or (None, why it can't be read)."""
    born, exam = _iso(date_of_birth), _iso(exam_date)
    if born is None:
        return None, "date of birth not readable"
    if exam is None:
        return None, "DMER exam date not readable"
    age = exam.year - born.year - ((exam.month, exam.day) < (born.month, born.day))
    if not _MIN_AGE <= age <= _MAX_AGE:
        return None, "date of birth or exam date appears misread"
    return age, None


def redact(normalized: dict) -> dict:
    """The normalized DMER as sent to the model (always unwrapped)."""
    fields = dict(fields_of(normalized))
    age, why = age_at_exam(
        fields.get("date_of_birth"), fields.get("medical_examination_date")
    )
    dropped = set(REDACTED_FIELDS)
    if (
        fields.get("endocrine.diabetes") is not True
        and fields.get("diabetes_guide_9_1") is not True
    ):
        dropped.add(_DIABETES_TREATMENT_FLAG)
    for name in dropped:
        fields.pop(name, None)
        fields.pop(f"{name}_evidence", None)
    fields["age_at_exam"] = age
    if why:
        fields["age_at_exam_note"] = why
    if isinstance(fields.get("field_sources"), dict):
        fields["field_sources"] = {
            k: v for k, v in fields["field_sources"].items() if k not in dropped
        }
    return fields


def checklist(sent: dict) -> list[str]:
    """Every condition field set to true in what is sent: the model must
    evaluate (or explicitly dismiss) each one, so none is silently skipped."""
    return sorted(
        name
        for name, value in sent.items()
        if value is True
        and "." in name
        and not name.startswith(_NOT_CONDITIONS)
        and not name.endswith(_CONCERN_SUFFIXES)
    )


def user_content(sent: dict, evaluation_date: date) -> list[dict]:
    """The user message's content parts. Parts, not a plain string: the AI Hub
    gateway masked a plain-string user message (POC)."""
    parts = [
        {
            "type": "text",
            "text": (
                f"Evaluation date: {evaluation_date.isoformat()}\n\n"
                "DMER (normalized.json):\n"
                + json.dumps(sent, indent=2, ensure_ascii=False)
            ),
        }
    ]
    required = checklist(sent)
    if required:
        parts.append(
            {
                "type": "text",
                "text": (
                    "Condition checklist - every field below is true in this DMER. "
                    'Each one must be cited (field_name: value) in the "evidence" or a '
                    'criterion\'s "evidence_reference" of at least one entry in '
                    '"conditions_evaluated". Evaluate every documented condition; if a '
                    "field is not a condition that needs triage on its own (e.g. a detail "
                    "of another condition), cite it under that condition, or add an entry "
                    "with rule_status NOT_APPLICABLE explaining why.\n"
                    + "\n".join(f"- {name}" for name in required)
                ),
            }
        )
    return parts
