"""The driver's details as read from the form, for Mercury's notes."""

import json

import pytest
from dmer_common.db.driver_lookup import DRIVER_NOT_FOUND, unidentified_driver_comment
from dmer_common.form_driver import (
    FormDriver,
    form_driver_from_combined,
    form_driver_note,
    read_form_driver,
)

COMBINED = {
    "document_id": "doc-1",
    "fields": {
        "dl_number": "123 4567",
        "first_name": "  Jo  Ann ",
        "last_name": "Doe",
        "exam_date": "2026-01-01",
    },
}


class _Blob:
    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.urls = []

    def blob_url(self, container, path):
        return f"https://data.blob/{container}/{path}"

    def download_if_exists(self, url):
        self.urls.append(url)
        if self.error:
            raise self.error
        return self.content


def test_the_details_come_from_combined_json_fields():
    details = form_driver_from_combined(json.dumps(COMBINED).encode())
    assert details == FormDriver(
        licence="123 4567", first_name="Jo Ann", last_name="Doe"
    )


@pytest.mark.parametrize(
    "content",
    [{}, {"fields": None}, {"fields": {}}, {"fields": {"first_name": "  "}}, []],
)
def test_missing_or_empty_fields_give_no_details(content):
    assert form_driver_from_combined(content) == FormDriver()


def test_the_note_names_every_part_the_form_gave():
    note = form_driver_note(
        FormDriver(licence="123 4567", first_name="Jo", last_name="Doe")
    )
    # a valid BC licence in its canonical form, as the pipeline looked it up
    assert note == (
        "Driver details as read from the DMER form: "
        "licence 01234567, first name Jo, surname Doe."
    )


def test_a_licence_that_isnt_a_bc_number_is_shown_as_read():
    note = form_driver_note(FormDriver(licence="AB12", last_name="Doe"))
    assert (
        note == "Driver details as read from the DMER form: licence AB12, surname Doe."
    )


@pytest.mark.parametrize("details", [None, FormDriver()])
def test_no_details_no_note(details):
    assert form_driver_note(details) == ""


def test_a_long_misread_is_cut_short():
    details = form_driver_from_combined({"fields": {"last_name": "x" * 500}})
    assert len(details.last_name) == 100


def test_read_form_driver_reads_the_documents_combined_json():
    blob = _Blob(json.dumps(COMBINED).encode())
    assert read_form_driver(blob, "doc-1").last_name == "Doe"
    assert blob.urls == ["https://data.blob/extracted-dmer/doc-1/combined.json"]


@pytest.mark.parametrize(
    "blob",
    [_Blob(None), _Blob(b"not json"), _Blob(error=RuntimeError("storage down"))],
    ids=["missing", "not-json", "unreadable"],
)
def test_read_form_driver_is_best_effort(blob):
    assert read_form_driver(blob, "doc-1") == FormDriver()


def test_read_form_driver_never_logs_the_details(caplog):
    caplog.set_level("DEBUG")
    read_form_driver(_Blob(json.dumps(COMBINED).encode()), "doc-1")
    read_form_driver(_Blob(error=RuntimeError("Doe")), "doc-1")
    assert "Doe" not in caplog.text and "4567" not in caplog.text


def test_the_driver_lookup_comment_carries_the_forms_details():
    comment = unidentified_driver_comment(
        DRIVER_NOT_FOUND,
        "01234567",
        FormDriver(licence="1234567", first_name="Jo", last_name="Doe"),
    )
    assert comment == (
        "Unable to AI process due to error. The driver's file could not be found "
        "or mapped in Mercury. Mercury has no driver with the page's licence. "
        "Driver details as read from the DMER form: "
        "licence 01234567, first name Jo, surname Doe."
    )


def test_without_the_form_the_driver_lookup_comment_keeps_the_licence_read():
    comment = unidentified_driver_comment(DRIVER_NOT_FOUND, "01234567")
    assert comment.endswith(
        "Driver details as read from the DMER form: licence 01234567."
    )


def test_without_any_details_the_driver_lookup_comment_has_the_reason_only():
    comment = unidentified_driver_comment(DRIVER_NOT_FOUND, None)
    assert comment.endswith("Mercury has no driver with the page's licence.")
