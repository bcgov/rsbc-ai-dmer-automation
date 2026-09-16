"""Local-only mock of the Mercury backlog API, for exercising intake-processor's
`dmer_intake` poller end to end without hitting the real Mercury service.

Run it alongside Azurite, the local Postgres container, and `func start` (see
../../README.md for the full local testing procedure):

    ../../.venv/Scripts/python.exe mock_mercury_api.py

Then point local.settings.json at it and restart `func start`:

    "MERCURY_API_BASE_URL": "http://localhost:8899/mercury/documents",
    "MERCURY_API_KEY": "local-dev-key"  # pragma: allowlist secret -- example value, not a real key

Shape of what it serves matches Mercury's real response: a `value` array of
DMER records, each with `document_guid` (-> dmer_id), a top-level
`document_url`, and an optional `driver` object with `licence_number` --
plus `documents`/`case` fields intake-processor currently ignores, included
here just to look like the real payload. Each record's `document_url` points
back at this same server, which serves a tiny dummy PDF there so
`_download_to_blob` has something real to fetch.

Two pages are served: page 1 (2 records, one with a driver and one without)
has a `nextLink` to page 2 (1 record, with a driver); page 2 has no
`nextLink`, so the next poll after that restarts the cycle at page 1 -- same
end-of-cycle behaviour as the real design. Because the same three
document_guids come back every cycle, restarting also exercises the dedup
path (`_already_known_dmer_ids` / `ON CONFLICT DO NOTHING`): after the first
full cycle, every subsequent poll should log 0 new DMERs.
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
            "document_guid": "MOCK-DMER-0001",
            "document_url": f"{BASE}/files/MOCK-DMER-0001.pdf",
            "driver": {"licence_number": "1234567"},
            "documents": [],
            "case": {},
        },
        {
            "document_guid": "MOCK-DMER-0002",
            "document_url": f"{BASE}/files/MOCK-DMER-0002.pdf",
            "driver": None,
            "documents": [],
            "case": {},
        },
    ],
    "nextLink": f"{BASE}/mercury/documents?queue=BOTH&page_size=50&cursor=page2",
}

_PAGE_2 = {
    "value": [
        {
            "document_guid": "MOCK-DMER-0003",
            "document_url": f"{BASE}/files/MOCK-DMER-0003.pdf",
            "driver": {"licence_number": "7654321"},
            "documents": [],
            "case": {},
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
