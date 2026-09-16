# mock-mercury-api — TEMPORARY test-only Azure Function App.
#
# Stands in for the real Mercury backlog API so intake-processor's
# `dmer_intake` poller can be exercised end-to-end against real Azure
# (VNet integration, Postgres AAD auth, real blob storage) without needing
# real Mercury credentials or a route from this dev machine into Mercury.
#
# This is NOT part of the DMER pipeline -- it exists solely so
# MERCURY_API_BASE_URL on func-rsbc-dmer-intake-processor-dev can point at
# something Azure can actually reach over the internet (a localhost mock,
# see ../mock_mercury_api.py, isn't reachable from Azure). Delete this
# Function App (and its storage account, stmockmercuryapi01) once Azure
# testing of the polling/advisory-lock refactor is done.
#
# Same two pages, same dedup-friendly fixed document_guids, same
# end-of-cycle restart behaviour as the local mock -- see that file's
# docstring for the full rationale.

from __future__ import annotations

import json

import azure.functions as func

app = func.FunctionApp()

_DUMMY_PDF = b"%PDF-1.4\n% Mock DMER PDF for Azure testing\n%%EOF\n"


def _pages(base_url: str) -> tuple[dict, dict]:
    page_1 = {
        "value": [
            {
                "document_guid": "MOCK-DMER-0001",
                "document_url": f"{base_url}/api/files/MOCK-DMER-0001.pdf",
                "driver": {"licence_number": "1234567"},
                "documents": [],
                "case": {},
            },
            {
                "document_guid": "MOCK-DMER-0002",
                "document_url": f"{base_url}/api/files/MOCK-DMER-0002.pdf",
                "driver": None,
                "documents": [],
                "case": {},
            },
        ],
        "nextLink": f"{base_url}/api/mercury/documents?queue=BOTH&page_size=50&cursor=page2",
    }
    page_2 = {
        "value": [
            {
                "document_guid": "MOCK-DMER-0003",
                "document_url": f"{base_url}/api/files/MOCK-DMER-0003.pdf",
                "driver": {"licence_number": "7654321"},
                "documents": [],
                "case": {},
            },
        ],
        "nextLink": None,
    }
    return page_1, page_2


@app.route(
    route="mercury/documents", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS
)
def mercury_documents(req: func.HttpRequest) -> func.HttpResponse:
    base_url = f"{req.url.split('/api/')[0]}"
    page_1, page_2 = _pages(base_url)
    cursor = req.params.get("cursor")
    page = page_2 if cursor == "page2" else page_1
    return func.HttpResponse(
        json.dumps(page), mimetype="application/json", status_code=200
    )


@app.route(route="files/{name}", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def files(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(_DUMMY_PDF, mimetype="application/pdf", status_code=200)
