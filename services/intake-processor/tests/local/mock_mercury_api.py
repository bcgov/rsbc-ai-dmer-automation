"""Local-only mock of the Mercury backlog API, for exercising intake-processor's
`dmer_poll` poller end to end without hitting the real Mercury service.

Run it alongside Azurite, the local Postgres container, and `func start` (see
../../README.md for the full local testing procedure):

    ../../.venv/Scripts/python.exe mock_mercury_api.py

Then point local.settings.json at it and restart `func start`:

    "MERCURY_API_BASE_URL": "http://localhost:8899/mercury/documents",
    "MERCURY_API_KEY": "local-dev-key"  # pragma: allowlist secret -- example value, not a real key

Shape of what it serves matches Mercury's real response: each record carries
the full set of fields Mercury actually returns (dps_queue, document_name,
document_type, document_status, document_priority, received_date,
document_type_business_area, queue, dmer_status, a driver object with
first/last name + licence_number + a documents[] history, and a case
object) -- intake-processor embeds all of it verbatim in the dmer-raw message
(see _publish_message, called from dmer_ingest), so none of it should be
dropped here just because intake-processor's own extraction logic only reads
document_guid/document_url/driver.licence_number today. `document_url` is
kept at the top level (not only nested inside driver.documents[]) since
that's what intake-processor currently reads to know what to download --
each one points back at this same server, which serves a tiny dummy PDF
there so `_download_source_pdf` has something real to fetch.

Two pages are served: page 1 (2 records, one with a driver and one without)
has a `nextLink` to page 2 (1 record, with a driver); page 2 has no
`nextLink`, so the next poll after that restarts the cycle at page 1 -- same
end-of-cycle behaviour as the real design. Because the same three
document_guids come back every cycle, restarting also exercises
`_upsert_document_and_driver`'s upsert-on-conflict path: the dmer_document
row is updated in place rather than duplicated, and a dmer-ingest message
gets republished for it every cycle regardless -- Service Bus's duplicate
detection window and dmer_ingest's own replay guard are what actually
suppress reprocessing here, not the poller skipping already-known records.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = "localhost"
PORT = 8899
BASE = f"http://{HOST}:{PORT}"

_DUMMY_PDF = b"%PDF-1.4\n% Mock DMER PDF for local testing\n%%EOF\n"

_PAGE_1 = {
    "value": [
        {
            "dps_queue": "General",
            "document_guid": "bd4c19d6-4e12-45ae-a82b-dfac1fb91bd1",
            "document_name": "DMER-0001 - MOCKONE.pdf",
            "document_type": "DMER",
            "document_status": "Uploaded",
            "document_priority": "Regular",
            "received_date": "2024-10-03T07:11:00Z",
            "document_type_business_area": "Driver Fitness",
            "queue": "Team - Intake",
            "dmer_status": "",
            "document_url": f"{BASE}/files/bd4c19d6-4e12-45ae-a82b-dfac1fb91bd1.pdf",
            "driver": {
                "driver_id": "D0001",
                "first_name": "Mock",
                "middle_name": "",
                "last_name": "One",
                "licence_number": "1234567",
                "documents": [
                    {
                        "file_name": "DMER-0001 - MOCKONE",
                        "document_type": "DMER",
                        "document_status": "Uploaded",
                        "uploaded_date": "2024-10-03T07:11:00Z",
                        "document_url": f"{BASE}/files/bd4c19d6-4e12-45ae-a82b-dfac1fb91bd1.pdf",
                        "dps_date": "",
                    }
                ],
            },
            "case": {
                "case_id": "C0001",
                "case_title": "0001 - MOCKONE - RSBC - 1",
                "case_type": "RSBC",
                "case_priority": "Regular",
                "case_owner": "Team - Intake",
                "case_status": "Open Pending Submission",
            },
        },
        {
            "dps_queue": "Unknown",
            "document_guid": "ccf30a7b-8598-4bdc-a3ee-b7dc17c74f9f",
            "document_name": "DMER-0002 - MOCKTWO.pdf",
            "document_type": "DMER",
            "document_status": "Uploaded",
            "document_priority": "Regular",
            "received_date": "2024-10-03T08:00:00Z",
            "document_type_business_area": "Driver Fitness",
            "queue": "Team - Intake",
            "dmer_status": "",
            "document_url": f"{BASE}/files/ccf30a7b-8598-4bdc-a3ee-b7dc17c74f9f.pdf",
            "driver": None,
            "case": {
                "case_id": "C0002",
                "case_title": "0002 - MOCKTWO - RSBC - 1",
                "case_type": "RSBC",
                "case_priority": "Regular",
                "case_owner": "Team - Intake",
                "case_status": "Open Pending Submission",
            },
        },
    ],
    "nextLink": f"{BASE}/mercury/documents?queue=BOTH&page_size=50&cursor=page2",
}

_PAGE_2 = {
    "value": [
        {
            "dps_queue": "General",
            "document_guid": "9e21b720-40c6-4fe2-937b-c0fe4534766d",
            "document_name": "DMER-0003 - MOCKTHREE.pdf",
            "document_type": "DMER",
            "document_status": "Uploaded",
            "document_priority": "Regular",
            "received_date": "2024-10-03T09:00:00Z",
            "document_type_business_area": "Driver Fitness",
            "queue": "Team - Intake",
            "dmer_status": "",
            "document_url": f"{BASE}/files/9e21b720-40c6-4fe2-937b-c0fe4534766d.pdf",
            "driver": {
                "driver_id": "D0003",
                "first_name": "Mock",
                "middle_name": "",
                "last_name": "Three",
                "licence_number": "7654321",
                "documents": [
                    {
                        "file_name": "DMER-0003 - MOCKTHREE",
                        "document_type": "DMER",
                        "document_status": "Uploaded",
                        "uploaded_date": "2024-10-03T09:00:00Z",
                        "document_url": f"{BASE}/files/9e21b720-40c6-4fe2-937b-c0fe4534766d.pdf",
                        "dps_date": "",
                    }
                ],
            },
            "case": {
                "case_id": "C0003",
                "case_title": "0003 - MOCKTHREE - RSBC - 1",
                "case_type": "RSBC",
                "case_priority": "Regular",
                "case_owner": "Team - Intake",
                "case_status": "Open Pending Submission",
            },
        },
    ],
    "nextLink": None,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[mock-mercury] {self.address_string()} - {fmt % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/mercury/documents":
            auth = self.headers.get("Authorization", "")
            cursor = query.get("cursor", [None])[0]
            page = _PAGE_2 if cursor == "page2" else _PAGE_1
            page_name = "page 2" if cursor == "page2" else "page 1"
            print(
                f"[mock-mercury] GET /mercury/documents (auth={'yes' if auth else 'NO'}, "
                f"queue={query.get('queue')}, cursor={cursor!r}) -> serving {page_name}: "
                f"{len(page['value'])} record(s), nextLink={'yes' if page['nextLink'] else 'no'}"
            )
            self._send_json(page)
            return

        if parsed.path.startswith("/files/") and parsed.path.endswith(".pdf"):
            print(
                f"[mock-mercury] GET {parsed.path} -> serving dummy PDF ({len(_DUMMY_PDF)} bytes)"
            )
            self._send_bytes(_DUMMY_PDF, "application/pdf")
            return

        print(f"[mock-mercury] GET {parsed.path} -> 404 (unknown mock endpoint)")
        self.send_error(404, "Unknown mock endpoint")

    def _send_json(self, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_bytes(self, payload: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[mock-mercury] Serving mock Mercury API on {BASE}")
    print(f"[mock-mercury]   MERCURY_API_BASE_URL = {BASE}/mercury/documents")
    print("[mock-mercury] Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock-mercury] Shutting down")
