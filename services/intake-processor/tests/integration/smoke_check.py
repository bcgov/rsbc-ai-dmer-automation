"""Manual local smoke test for the intake-processor blob trigger.

Not a pytest test (named to avoid pytest's test_*.py/*_test.py auto-collection)
-- run it directly, with Azurite and `func start` already running, to
exercise the pipeline end to end:

    ../../.venv/Scripts/python.exe smoke_check.py

It creates the "raw" container in the local Azurite emulator (if missing)
and uploads a blob named "DMER0001 - A1234-56789-01234.pdf" to it, which
should trigger `dmer_intake` in the running Functions host and insert a row
into `dmer_processing` with status="started".
"""

from azure.storage.blob import BlobServiceClient

# Well-known Azurite development connection string (fixed local emulator
# account name/key -- not a secret).
AZURITE_CONNECTION_STRING = (
    "DefaultEndpointsProtocol=http;"
    "AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
)

CONTAINER_NAME = "raw"
BLOB_NAME = "DMER0001 - 1234567890.pdf"  # driver's licence numbers are typically 8-10 digits


def main() -> None:
    client = BlobServiceClient.from_connection_string(AZURITE_CONNECTION_STRING)

    container = client.get_container_client(CONTAINER_NAME)
    if not container.exists():
        container.create_container()
        print(f"Created container '{CONTAINER_NAME}'")

    blob = container.get_blob_client(BLOB_NAME)
    blob.upload_blob(b"%PDF-1.4 fake DMER content for local smoke test", overwrite=True)
    print(f"Uploaded blob '{BLOB_NAME}' to container '{CONTAINER_NAME}'")
    print("Check the running `func start` logs for the dmer_intake trigger,")
    print("then query dmer_processing for dmer_id='DMER0001'.")


if __name__ == "__main__":
    main()
