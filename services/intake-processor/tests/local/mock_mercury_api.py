"""Local-only mock of Mercury's GET dmers API, for exercising intake-processor's
`dmer_poll` poller end to end without hitting the real Mercury service.

Run it alongside Azurite, the local Postgres container, and `func start` (see
../../README.md for the full local testing procedure):

    ../../.venv/Scripts/python.exe mock_mercury_api.py

Then point local.settings.json at it and restart `func start`:

    "MERCURY_API_BASE_URL": "http://localhost:8899/Dmers",
    "MERCURY_AUTH_TOKEN_URL": "http://localhost:8899/Authentication/token",
    "MERCURY_API_KEY": "local-dev-key"  # pragma: allowlist secret -- example value, not a real key

What it serves matches Mercury's GET dmers shape (camelCase `items`): three
DMERs (one without a driver) and one non-DMER (an HRDIR report with no
documentUrl, which the poller skips but still marks). As in Mercury, a
DMER's documentUrl is a relative path, not a link; the file comes from
GET /Dmers/{documentRecordId}/files, which serves a tiny dummy PDF so
`_download_source_pdf` has something real to fetch.

Claim-based, like Mercury: GET /Dmers?pageSize=N returns the first N records
not yet marked; POST /Dmers/BatchSetDmerAIProccessedStatus {"dmerIds": [...]}
marks them, and the next GET returns the ones after. Marks are kept in
memory -- restart this script to start over (or POST /reset).
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = "localhost"
PORT = 8899
BASE = f"http://{HOST}:{PORT}"

_DUMMY_PDF = b"%PDF-1.4\n% Mock DMER PDF for local testing\n%%EOF\n"


def _case(case_id: str, title: str) -> dict:
    return {
        "caseId": case_id,
        "caseTitle": title,
        "caseType": "Suspected Medical Condition",
        "casePriority": "Regular",
        "caseOwner": "Team - Intake",
        "caseStatus": "Open Pending Submission",
    }


def _driver(driver_id: str, last_name: str, licence: str) -> dict:
    return {
        "driverId": driver_id,
        "firstName": "Mock",
        "middleName": "",
        "lastName": last_name,
        "licenceNumber": licence,
        "documents": [],
    }


def _dmer(guid: str, name: str, received: str, driver: dict | None, case: dict) -> dict:
    return {
        "documentRecordId": guid,
        "queue": "Team - Intake",
        "documentName": name,
        "documentUrl": f"dfp_driver/mock/{guid}.PDF",
        "documentType": "DMER",
        "submittalStatus": "Uploaded",
        "documentPriority": "Regular",
        "receivedDate": received,
        "documentTypeBusinessArea": "Driver Fitness",
        "dmerStatus": "Uploaded",
        "driver": driver,
        "dmerDocumentCaseRecord": case,
    }


_RECORDS = [
    _dmer(
        "bd4c19d6-4e12-45ae-a82b-dfac1fb91bd1",
        "DMER-0001 - MOCKONE.pdf",
        "10/03/2024 00:11:00",
        _driver("D0001", "One", "1234567"),
        _case("C0001", "0001 - MOCKONE - DMER - 1"),
    ),
    _dmer(
        "ccf30a7b-8598-4bdc-a3ee-b7dc17c74f9f",
        "DMER-0002 - MOCKTWO.pdf",
        "10/03/2024 01:00:00",
        None,
        _case("C0002", "0002 - MOCKTWO - DMER - 1"),
    ),
    {
        "documentRecordId": "05382879-02d0-ed11-b841-00505683fbf4",
        "queue": "Team - Intake",
        "documentName": "HRDIR High Risk Driving Incident Report-0003 - MOCKTHREE .",
        "documentUrl": "",
        "documentType": "HRDIR High Risk Driving Incident Report",
        "submittalStatus": "Uploaded",
        "documentPriority": "Critical Review",
        "receivedDate": "10/03/2024 01:30:00",
        "documentTypeBusinessArea": "",
        "dmerStatus": "Uploaded",
        "driver": _driver("D0003", "Three", "7654321"),
        "dmerDocumentCaseRecord": _case("C0003", "0003 - MOCKTHREE - DIP - 1"),
    },
    _dmer(
        "9e21b720-40c6-4fe2-937b-c0fe4534766d",
        "DMER-0003 - MOCKTHREE.pdf",
        "10/03/2024 02:00:00",
        _driver("D0003", "Three", "7654321"),
        _case("C0003", "0003 - MOCKTHREE - DMER - 1"),
    ),
]

_marked: set[str] = set()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[mock-mercury] {self.address_string()} - {fmt % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path.lower() == "/authentication/token":
            ok = bool(query.get("secret", [""])[0])
            print(f"[mock-mercury] GET /Authentication/token -> {200 if ok else 401}")
            if not ok:
                self.send_error(401, "secret required")
                return
            self._send_json({"token": "local-mock-token", "expiresIn": 3600})
            return

        if parsed.path.lower() == "/dmers":
            auth = self.headers.get("Authorization", "")
            page_size = int(query.get("pageSize", ["50"])[0])
            waiting = [r for r in _RECORDS if r["documentRecordId"] not in _marked]
            items = waiting[:page_size]
            print(
                f"[mock-mercury] GET /Dmers (auth={'yes' if auth else 'NO'}, "
                f"pageSize={page_size}) -> {len(items)} record(s), "
                f"{len(_marked)} marked so far"
            )
            # Like Mercury: no nextLink; count echoes the page size.
            self._send_json({"items": items, "count": page_size})
            return

        parts = parsed.path.strip("/").split("/")
        if len(parts) == 3 and parts[0].lower() == "dmers" and parts[2] == "files":
            known = {r["documentRecordId"] for r in _RECORDS if r["documentUrl"]}
            if parts[1] not in known:
                print(f"[mock-mercury] GET /Dmers/{parts[1]}/files -> 404")
                self.send_error(404, "Unknown document")
                return
            print(
                f"[mock-mercury] GET /Dmers/{parts[1]}/files -> "
                f"serving dummy PDF ({len(_DUMMY_PDF)} bytes)"
            )
            self._send_bytes(_DUMMY_PDF, "application/pdf")
            return

        print(f"[mock-mercury] GET {parsed.path} -> 404 (unknown mock endpoint)")
        self.send_error(404, "Unknown mock endpoint")

    def do_POST(self):
        path = urlparse(self.path).path.lower()
        if path == "/dmers/batchsetdmeraiproccessedstatus":
            length = int(self.headers.get("Content-Length", "0"))
            ids = json.loads(self.rfile.read(length) or b"{}").get("dmerIds") or []
            _marked.update(ids)
            print(
                f"[mock-mercury] POST BatchSetDmerAIProccessedStatus -> {len(ids)} marked"
            )
            self._send_json({"ok": True, "updated": len(ids)})
            return
        if path == "/reset":
            _marked.clear()
            print("[mock-mercury] POST /reset -> every record is waiting again")
            self._send_json({"ok": True})
            return
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
    print(f"[mock-mercury]   MERCURY_API_BASE_URL = {BASE}/Dmers")
    print(f"[mock-mercury]   MERCURY_AUTH_TOKEN_URL = {BASE}/Authentication/token")
    print("[mock-mercury] Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock-mercury] Shutting down")
