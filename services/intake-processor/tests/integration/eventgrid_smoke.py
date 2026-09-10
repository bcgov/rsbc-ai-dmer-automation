"""Manual local smoke test for the Event Grid-triggered intake function.

`dmer_intake` is an `@app.event_grid_trigger` -- it reads a
`Microsoft.Storage.BlobCreated` event and never downloads the blob. Locally
there is no Event Grid, so this script POSTs a synthetic event straight to
the Functions host's Event Grid webhook, which routes it to the function.

Run with `func start` already running (Azurite must be up too, since the host
uses it for AzureWebJobsStorage), then:

    <py-3.10> eventgrid_smoke.py

Then check the `func start` console and query `dmer_processing` for
dmer_id='DMER0001'.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

CONTAINER = "incoming-dmer-queue"
BLOB = "DMER0001 - 1234567890.pdf"
FUNCTION_NAME = "dmer_intake"
WEBHOOK_URL = f"http://localhost:7071/runtime/webhooks/eventgrid?functionName={FUNCTION_NAME}"
STORAGE_ACCOUNT = "rsbcstorage"


def build_event() -> dict:
    return {
        "topic": (
            "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/"
            f"local/providers/Microsoft.Storage/storageAccounts/{STORAGE_ACCOUNT}"
        ),
        "subject": f"/blobServices/default/containers/{CONTAINER}/blobs/{BLOB}",
        "eventType": "Microsoft.Storage.BlobCreated",
        "id": str(uuid.uuid4()),
        "data": {
            "api": "PutBlob",
            "requestId": str(uuid.uuid4()),
            "eTag": "0x8D0000000000000",
            "contentType": "application/pdf",
            "contentLength": 45,
            "blobType": "BlockBlob",
            "url": (
                f"https://{STORAGE_ACCOUNT}.blob.core.windows.net/{CONTAINER}/"
                + urllib.parse.quote(BLOB)
            ),
            "sequencer": "0" * 64,
            "storageDiagnostics": {"batchId": str(uuid.uuid4())},
        },
        "dataVersion": "",
        "metadataVersion": "1",
        # Match the format real Event Grid sends: UTC with a trailing 'Z'.
        # The Functions worker's EventGridEvent decoder rejects offset forms
        # like '-07:00'.
        "eventTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
    }


def main() -> None:
    body = json.dumps([build_event()]).encode()
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "aeg-event-type": "Notification",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"POST {WEBHOOK_URL} -> {resp.status}")
            text = resp.read().decode().strip()
            if text:
                print(text)
    except urllib.error.HTTPError as exc:
        print(f"POST {WEBHOOK_URL} -> {exc.code} {exc.reason}")
        body = exc.read().decode().strip()
        if body:
            print(body)
    print("Check the `func start` console and dmer_processing for dmer_id='DMER0001'.")


if __name__ == "__main__":
    main()
