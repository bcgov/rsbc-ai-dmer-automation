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
# end-of-cycle restart behaviour, and same full Mercury-shaped records
# (dps_queue/document_name/.../driver/case) as the local mock -- see that
# file's docstring for the full rationale on why every field is included
# here even though intake-processor's own extraction logic only reads
# document_guid/document_url/driver.licence_number today: the rest gets
# embedded verbatim in the raw-dmer-queue message.

from __future__ import annotations

import json

import azure.functions as func

app = func.FunctionApp()

_DUMMY_PDF = b"%PDF-1.4\n% Mock DMER PDF for Azure testing\n%%EOF\n"


def _pages(base_url: str) -> tuple[dict, dict]:
    page_1 = {
        "value": [
            {
                "dps_queue": "General",
                "document_guid": "258a9a03-ee50-4363-8519-ae546fab4add",
                "document_name": "DMER-1001 - MOCKONE.pdf",
                "document_type": "DMER",
                "document_status": "Uploaded",
                "document_priority": "Regular",
                "received_date": "2024-10-03T07:11:00Z",
                "document_type_business_area": "Driver Fitness",
                "queue": "Team - Intake",
                "dmer_status": "",
                "document_url": f"{base_url}/api/files/258a9a03-ee50-4363-8519-ae546fab4add.pdf",
                "driver": {
                    "driver_id": "D2001",
                    "first_name": "Mock",
                    "middle_name": "",
                    "last_name": "One",
                    "licence_number": "1234567",
                    "documents": [
                        {
                            "file_name": "DMER-1001 - MOCKONE",
                            "document_type": "DMER",
                            "document_status": "Uploaded",
                            "uploaded_date": "2024-10-03T07:11:00Z",
                            "document_url": f"{base_url}/api/files/258a9a03-ee50-4363-8519-ae546fab4add.pdf",
                            "dps_date": "",
                        }
                    ],
                },
                "case": {
                    "case_id": "C2001",
                    "case_title": "1001 - MOCKONE - RSBC - 1",
                    "case_type": "RSBC",
                    "case_priority": "Regular",
                    "case_owner": "Team - Intake",
                    "case_status": "Open Pending Submission",
                },
            },
            {
                "dps_queue": "Unknown",
                "document_guid": "13fd5328-796a-487b-8ea3-ad6ac31135c2",
                "document_name": "DMER-1002 - MOCKTWO.pdf",
                "document_type": "DMER",
                "document_status": "Uploaded",
                "document_priority": "Regular",
                "received_date": "2024-10-03T08:00:00Z",
                "document_type_business_area": "Driver Fitness",
                "queue": "Team - Intake",
                "dmer_status": "",
                "document_url": f"{base_url}/api/files/13fd5328-796a-487b-8ea3-ad6ac31135c2.pdf",
                "driver": None,
                "case": {
                    "case_id": "C2002",
                    "case_title": "1002 - MOCKTWO - RSBC - 1",
                    "case_type": "RSBC",
                    "case_priority": "Regular",
                    "case_owner": "Team - Intake",
                    "case_status": "Open Pending Submission",
                },
            },
        ],
        "nextLink": f"{base_url}/api/mercury/documents?queue=BOTH&page_size=50&cursor=page2",
    }
    page_2 = {
        "value": [
            {
                "dps_queue": "General",
                "document_guid": "8d4068b0-c45d-497e-a57d-62aea31d112a",
                "document_name": "DMER-1003 - MOCKTHREE.pdf",
                "document_type": "DMER",
                "document_status": "Uploaded",
                "document_priority": "Regular",
                "received_date": "2024-10-03T09:00:00Z",
                "document_type_business_area": "Driver Fitness",
                "queue": "Team - Intake",
                "dmer_status": "",
                "document_url": f"{base_url}/api/files/8d4068b0-c45d-497e-a57d-62aea31d112a.pdf",
                "driver": {
                    "driver_id": "D2003",
                    "first_name": "Mock",
                    "middle_name": "",
                    "last_name": "Three",
                    "licence_number": "7654321",
                    "documents": [
                        {
                            "file_name": "DMER-1003 - MOCKTHREE",
                            "document_type": "DMER",
                            "document_status": "Uploaded",
                            "uploaded_date": "2024-10-03T09:00:00Z",
                            "document_url": f"{base_url}/api/files/8d4068b0-c45d-497e-a57d-62aea31d112a.pdf",
                            "dps_date": "",
                        }
                    ],
                },
                "case": {
                    "case_id": "C2003",
                    "case_title": "1003 - MOCKTHREE - RSBC - 1",
                    "case_type": "RSBC",
                    "case_priority": "Regular",
                    "case_owner": "Team - Intake",
                    "case_status": "Open Pending Submission",
                },
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
