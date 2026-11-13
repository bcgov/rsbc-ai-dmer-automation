"""Tests for the triage LLM: what is sent (dmer_common.triage.request), how the
answer is validated and checked, and how it becomes an outcome
(dmer_common.triage.answer -> the rules engine's select_outcome), plus one
call end to end with a stand-in model (dmer_common.triage.engine)."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import Mock

import pytest
from dmer_common.normalization import NormalizationValidationError
from dmer_common.normalization.errors import InvalidModelOutput
from dmer_common.rules import select_outcome
from dmer_common.triage import PolicyText, triage_document
from dmer_common.triage.answer import checks, outcomes, validate
from dmer_common.triage.prompt import system_prompt
from dmer_common.triage.request import REDACTED_FIELDS, checklist, redact, user_content

# ---------------------------------------------------------------------------
# What is sent
# ---------------------------------------------------------------------------

IDENTITY = {
    "first_name": "Mary",
    "last_name": "Thompson",
    "dl_number": "1234567",
    "date_of_birth": "1950-03-15",
    "physician_or_np_fax_present": "Dr. A. Smith, 100 Main St, 250-555-0100",
    "scan_from_information": "FAX 250-555-0199",
    "relationship_with_patient.name_and_address": "Dr. A. Smith, 100 Main St",
    "doctor_signature": "signed",
}


def _dmer(**fields) -> dict:
    return {
        **IDENTITY,
        **{f"{name}_evidence": f'{name}: "x"' for name in ("first_name", "dl_number")},
        "medical_examination_date": "2026-03-14",
        "current_licence_class": "5",
        "cardiovascular.atrial_fibrillation": True,
        "field_sources": {
            "first_name": "di",
            "dl_number": "di",
            "cardiovascular.atrial_fibrillation": "di",
        },
        **fields,
    }


def test_no_identity_field_or_its_evidence_is_sent():
    sent = redact(_dmer())
    for name in REDACTED_FIELDS:
        assert name not in sent and f"{name}_evidence" not in sent
        assert name not in sent["field_sources"]
    text = json.dumps(user_content(sent, date(2026, 11, 9)))
    for value in IDENTITY.values():
        assert value not in text


def test_every_identifying_looking_field_is_redacted():
    # A new identity field in the normalized schema must be added to
    # REDACTED_FIELDS; this fails if one is sent unnoticed.
    sent = redact(_dmer(**{"patient.phone": "250-555-0123"}))
    identifying = ("name", "dl_number", "birth", "address", "phone", "signature", "fax")
    leaked = [
        k
        for k in sent
        if any(h in k.lower() for h in identifying) and k != "field_sources"
    ]
    assert leaked == [
        "patient.phone"
    ]  # the test catches it -- now drop it from the list
    sent = redact(_dmer())
    assert [k for k in sent if any(h in k.lower() for h in identifying)] == []


@pytest.mark.parametrize(
    "dob, exam, age, note",
    [
        ("1950-03-15", "2026-03-14", 75, None),  # the day before the birthday
        ("1950-03-14", "2026-03-14", 76, None),  # on the birthday
        ("", "2026-03-14", None, "date of birth not readable"),
        ("1950-03-15", "", None, "DMER exam date not readable"),
        (
            "2020-01-01",
            "2026-03-14",
            None,
            "date of birth or exam date appears misread",
        ),
    ],
)
def test_date_of_birth_is_sent_as_age_at_exam(dob, exam, age, note):
    sent = redact(_dmer(date_of_birth=dob, medical_examination_date=exam))
    assert "date_of_birth" not in sent
    assert sent["age_at_exam"] == age
    assert sent.get("age_at_exam_note") == note


def test_the_diabetes_treatment_flag_is_sent_only_with_diabetes():
    flag = "endocrine.diabetes_treatment_not_indicated"
    assert flag not in redact(_dmer(**{flag: True}))
    assert redact(_dmer(**{flag: True, "endocrine.diabetes": True}))[flag] is True
    assert redact(_dmer(**{flag: True, "diabetes_guide_9_1": True}))[flag] is True


def test_a_wrapped_normalized_dmer_is_read_the_same():
    assert redact({"dmer": _dmer()}) == redact(_dmer())


def test_checklist_lists_true_conditions_only():
    sent = redact(
        _dmer(
            **{
                "cardiovascular.atrial_fibrillation_has_concerns": True,
                "opinion.no": True,
                "visual_acuity.vision_20/50_or_better": True,
                "cns.epilepsy": False,
                "no_other_conditions": True,  # no category: not a condition field
            }
        )
    )
    assert checklist(sent) == ["cardiovascular.atrial_fibrillation"]


def test_user_message_is_content_parts_with_the_checklist():
    parts = user_content(redact(_dmer()), date(2026, 11, 9))
    assert [p["type"] for p in parts] == ["text", "text"]
    assert parts[0]["text"].startswith(
        "Evaluation date: 2026-11-09\n\nDMER (normalized.json):\n{"
    )
    assert parts[1]["text"].endswith("- cardiovascular.atrial_fibrillation")


def test_system_prompt_names_the_procedures_version_and_ends_with_them():
    prompt = system_prompt("POLICY TEXT", date(2026, 6, 12))
    assert "updated June 12, 2026**" in prompt
    assert '"version": "2026-06-12"' in prompt
    assert prompt.endswith(
        "## Triage Sort Procedures (authoritative source)\n\nPOLICY TEXT"
    )
    assert "{" not in prompt.split("## 6. Required output format")[0].replace(
        "{policy", ""
    )


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------


def _condition(action=None, status=None, **extra) -> dict:
    return {
        "condition": extra.pop("name", "Atrial fibrillation"),
        "rule_status": status or ("APPLIED" if action else "NOT_APPLICABLE"),
        "recommended_action": action,
        "fit_letter": extra.pop("fit", False),
        "policy_reference": {
            "chapter": "Guide Sec. 4 Chapter 3: Cardiovascular",
            "page": extra.pop("page", 4),
            "condition_or_rule": extra.pop("rule", "Arrhythmia - no concerns"),
        },
        "evidence": extra.pop("evidence", ["cardiovascular.atrial_fibrillation: true"]),
        **extra,
    }


def _answer(*conditions, status="RULE_MATCHED", overall="auto") -> dict:
    if overall == "auto":  # what a consistent model would give
        order = ["CP", "IN", "PR", "PU", "TCM", "CR"]
        levels = [
            str(c["recommended_action"]).rstrip("*")
            for c in conditions
            if str(c["recommended_action"]).rstrip("*") in order
        ]
        overall = max(levels, key=order.index) if levels else None
    return {
        "evaluation_status": status,
        "conditions_evaluated": list(conditions),
        "overall_recommended_action": overall,
    }


def _selected(answer: dict, sent: dict | None = None):
    return select_outcome(outcomes(validate(answer), sent or {}))


@pytest.mark.parametrize(
    "answer, message",
    [
        (
            {"evaluation_status": "DONE", "conditions_evaluated": []},
            "evaluation_status",
        ),
        (
            {"evaluation_status": "RULE_MATCHED", "conditions_evaluated": "x"},
            "list of objects",
        ),
        (_answer(_condition("XX")), "not a policy code"),
        (
            _answer(_condition(None, status="APPLIED")),
            "rule_status APPLIED with no action",
        ),
        (_answer(_condition("CP", status="NOT_APPLICABLE")), "with an action"),
        (_answer(_condition("CP", status="MAYBE")), "unknown rule_status"),
    ],
)
def test_an_answer_select_outcome_cannot_read_is_invalid(answer, message):
    with pytest.raises(InvalidModelOutput, match=message):
        validate(answer)


def test_the_highest_applied_action_wins():
    evaluation = _selected(_answer(_condition("CP"), _condition("PU", name="Seizure")))
    assert evaluation.selected_outcome_code == "PU"


def test_an_asterisked_action_reads_as_its_code():
    evaluation = _selected(_answer(_condition("IN*", fit=True)))
    assert evaluation.selected_outcome_code == "IN"
    assert evaluation.fit_letter is True


def test_the_fit_letter_comes_from_the_winning_action_only():
    evaluation = _selected(
        _answer(_condition("IN", fit=True), _condition("PR", name="Syncope"))
    )
    assert (evaluation.selected_outcome_code, evaluation.fit_letter) == ("PR", False)


def test_a_policy_gap_is_at_least_intake():
    evaluation = _selected(
        _answer(
            _condition("CP"),
            _condition(None, status="POLICY_GAP", name="MoCA 24"),
            status="HUMAN_REVIEW_REQUIRED",
        )
    )
    assert evaluation.selected_outcome_code == "IN"
    assert (
        "no rule that fits the documented condition (MoCA 24)"
        in evaluation.selected_reason
    )
    assert (
        "asked for human review" not in evaluation.selected_reason
    )  # the gap already says why


def test_a_request_for_review_is_at_least_intake():
    evaluation = _selected(_answer(_condition("CP"), status="HUMAN_REVIEW_REQUIRED"))
    assert evaluation.selected_outcome_code == "IN"
    assert "asked for human review" in evaluation.selected_reason


def test_a_higher_action_stands_over_a_request_for_review():
    evaluation = _selected(_answer(_condition("PU"), status="HUMAN_REVIEW_REQUIRED"))
    assert evaluation.selected_outcome_code == "PU"


def test_no_applied_rule_is_intake_as_with_the_rules_engine():
    evaluation = _selected(_answer(_condition(None)))
    assert evaluation.selected_outcome_code == "IN"
    assert evaluation.selected_reason == "No triage rule matched the DMER"


@pytest.mark.parametrize(
    "fields",
    [
        {"psychotropic_drugs.perscribed_drugs": True},
        {"psychotropic_drugs.substance_abuse_or_dependence": True},
        {
            "psychiatric.on_medications": True,
            "psychiatric.compliant_with_treatment": True,
        },
        {
            "psychiatric.on_medications": True,
            "psychiatric.non_compliant_with_treatment": True,
        },
    ],
)
def test_a_clean_pass_with_a_driving_record_rule_goes_to_intake(fields):
    evaluation = _selected(_answer(_condition("CP")), fields)
    assert evaluation.selected_outcome_code == "IN"
    assert evaluation.rule_engine_outcome_code == "CP"
    assert "driving record must be checked" in evaluation.selected_reason


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {
            "psychiatric.on_medications": True
        },  # no compliance tick: no rules-engine row fires
        {
            "psychotropic_drugs.perscribed_drugs_compliant": True
        },  # without the drugs box
    ],
)
def test_the_driving_record_check_uses_the_rules_engines_fields_only(fields):
    # Even when the model applied a drug rule itself (POC DMER5/16: stays CP).
    drug_rule = _condition(
        "CP", name="Prescribed medications", rule="Prescribed medications - compliant"
    )
    drug_rule["policy_reference"][
        "chapter"
    ] = "Guide Sec. 13/14 Chapter 15: Drugs, Alcohol and Driving"
    evaluation = _selected(_answer(drug_rule), fields)
    assert evaluation.selected_outcome_code == "CP"


def test_a_higher_action_stands_over_the_driving_record_check():
    evaluation = _selected(
        _answer(_condition("PR")), {"psychotropic_drugs.perscribed_drugs": True}
    )
    assert evaluation.selected_outcome_code == "PR"


def test_checks_name_what_does_not_match_the_input():
    sent = {
        "cardiovascular.atrial_fibrillation": True,
        "visual_acuity.vision_20/50_or_better": True,
    }
    answer = _answer(
        _condition(
            "CP",
            page=57,
            evidence=[
                "cardiovascular.atrial_fibrillation: true",  # matches
                "visual_acuity.vision_20/50_or_better: true",  # matches (a "/" in the name)
                "cns.epilepsy: true",  # not sent
                'details_of_condition: "angina"',  # not sent
            ],
        ),
        overall="PU",
    )
    problems = checks(answer, sent)
    assert "condition 0: cites a page that does not exist" in problems
    assert "condition 0: cites unknown field cns.epilepsy" in problems
    assert "condition 0: cites unknown field details_of_condition" in problems
    assert "overall action is not the highest condition action" in problems
    assert not any("vision_20/50" in p or "atrial" in p for p in problems)


def test_checks_catch_a_quote_that_is_not_in_the_value():
    sent = {"details_of_condition": "Atrial fibrillation since 2019, stable."}
    good = _answer(
        _condition(
            "CP", evidence=['details_of_condition: "Atrial fibrillation ... stable."']
        )
    )
    bad = _answer(
        _condition("CP", evidence=['details_of_condition: "Atrial flutter, unstable"'])
    )
    assert checks(good, sent) == []
    assert checks(bad, sent) == [
        "condition 0: evidence for details_of_condition does not match the input"
    ]


# ---------------------------------------------------------------------------
# One call, end to end
# ---------------------------------------------------------------------------

POLICY = PolicyText(
    text="PROCEDURES", pdf_sha256="ab" * 32, pdf_etag='"0x1"', updated=date(2026, 6, 12)
)


def _model(*answers: dict) -> Mock:
    client = Mock()
    client.complete.side_effect = [json.dumps(a) for a in answers]
    return client


def test_triage_document_returns_the_selected_outcome_and_its_record():
    client = _model(
        _answer(_condition("PR", fit=True, rule="Arrhythmia - with concerns"))
    )

    result = triage_document(client, _dmer(), POLICY, evaluation_date=date(2026, 11, 9))

    assert result.evaluation.selected_outcome_code == "PR"
    assert result.evaluation.fit_letter is True
    record = result.record()
    assert record["engine"] == "triage_llm"
    assert record["policy_updated"] == "2026-06-12"
    assert record["candidates"][0]["action"] == "PR"
    assert record["conditions"][0]["rule_status"] == "APPLIED"
    assert "evidence" not in record["conditions"][0] and "reasoning" not in json.dumps(
        record
    )
    sent = client.complete.call_args.kwargs["messages"]
    assert sent[0]["content"].endswith("PROCEDURES")
    assert "Mary" not in json.dumps(sent[1]["content"])
    assert (
        "temperature" not in client.complete.call_args.kwargs
    )  # the deployment's default


def test_an_invalid_answer_is_retried():
    client = _model(
        _answer(_condition(None, status="APPLIED")), _answer(_condition("CP"))
    )

    result = triage_document(client, _dmer(), POLICY, evaluation_date=date(2026, 11, 9))

    assert client.complete.call_count == 2
    assert result.evaluation.selected_outcome_code == "CP"


def test_an_answer_still_invalid_after_its_retries_is_poison():
    client = _model(*[_answer(_condition("XX"))] * 3)

    with pytest.raises(NormalizationValidationError):
        triage_document(client, _dmer(), POLICY, evaluation_date=date(2026, 11, 9))


def test_no_dmer_text_reaches_the_logs_from_a_triage_call(monkeypatch):
    """A Section D sentinel sent to the model and quoted back in its answer
    must not appear in any log record (health information under FOIPPA)."""
    from dmer_common.normalization import errors, unmask
    from dmer_common.triage import engine

    sentinel = "SENTINEL-SECTION-D-9c41"
    log = Mock()
    for module in (errors, unmask, engine):
        monkeypatch.setattr(module, "_log", log)
    answer = _answer(
        _condition(
            "PR",
            evidence=[f'details_of_condition: "{sentinel} dizzy ******"'],
            reasoning=f"{sentinel} with symptoms",
            name=f"{sentinel} condition",
        )
    )
    answer[f"{sentinel} key"] = "made-up key"

    triage_document(
        _model(answer),
        _dmer(details_of_condition=f"{sentinel} dizzy spells"),
        POLICY,
        evaluation_date=date(2026, 11, 9),
    )

    records = [repr((c.args, c.kwargs)) for c in log.method_calls]
    assert records, "expected the triage call to be logged"
    assert not any(sentinel in r for r in records)
