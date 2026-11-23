"""Mercury add_case helpers: placeholder cases and open-case detection."""

import pytest
from dmer_common.mercury_client.case_link import is_placeholder_case, open_case_id


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ({"caseTitle": "00000000- DFC- DMER- 1"}, True),
        ({"caseTitle": "00000000- DFC- OTHR- 4"}, True),
        ({"caseTitle": "00000000 DFC"}, True),  # starts with 00000000
        ({"case_title": " 00000000 - DFC - DMER - 2"}, True),
        ({"caseTitle": "0000000- DFC- DMER- 2"}, False),  # only 00000000
        ({"caseTitle": "01234567- DRIVER- DMER- 1"}, False),
        # zeros inside a real licence are not a placeholder
        ({"caseTitle": "10000000- DRIVER- DMER- 1"}, False),
        ({"caseTitle": ""}, False),
        (None, False),
    ],
)
def test_placeholder_case_is_recognised_by_its_title(case, expected):
    assert is_placeholder_case(case) is expected


def test_open_case_id_ignores_placeholder_and_closed_cases():
    placeholder = {"case_id": "C0", "case_title": "00000000- DFC- DMER- 1"}
    assert open_case_id({"case": placeholder}) is None
    assert open_case_id({"case": {"case_id": "C1", "case_status": "Closed"}}) is None
    assert open_case_id({"case": {"case_id": "C2", "case_status": "Active"}}) == "C2"
    assert open_case_id({"case": {"case_id": "C3"}}) == "C3"  # no status: open
