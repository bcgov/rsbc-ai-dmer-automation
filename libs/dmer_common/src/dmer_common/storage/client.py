"""Managed-Identity-authenticated Blob Storage client.

Thin wrapper over ``azure-storage-blob`` so services never open a raw SDK client
directly. Authentication uses ``DefaultAzureCredential`` (user-assigned Managed
Identity in Azure). JSON helpers centralize (de)serialization and content type.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

_JSON_CONTENT = ContentSettings(content_type="application/json")


class BlobClient:
    """Blob operations scoped to a single storage account.

    Parameters
    ----------
    account_url:
        e.g. ``https://<account>.blob.core.windows.net``.
    credential:
        Optional credential; defaults to ``DefaultAzureCredential`` (Managed
        Identity). Injectable for tests.
    """

    def __init__(self, account_url: str, credential: Any | None = None) -> None:
        self._account_url = account_url.rstrip("/")
        self._service = BlobServiceClient(
            account_url=self._account_url,
            credential=credential or DefaultAzureCredential(),
        )

    def blob_url(self, container: str, path: str) -> str:
        """Return the full https URL for a blob within this account."""
        return f"{self._account_url}/{container}/{path.lstrip('/')}"

    def download(self, uri: str) -> bytes:
        """Download a blob by full URL (``https://.../container/path``)."""
        container, path = self._split_uri(uri)
        blob = self._service.get_blob_client(container=container, blob=path)
        return blob.download_blob().readall()

    def upload_json(self, container: str, path: str, obj: Any) -> str:
        """Serialize ``obj`` to JSON, upload it, and return the blob URL."""
        data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        return self.upload_bytes(container, path, data, content_settings=_JSON_CONTENT)

    def upload_bytes(
        self,
        container: str,
        path: str,
        data: bytes,
        *,
        content_settings: ContentSettings | None = None,
    ) -> str:
        """Upload raw bytes (overwriting) and return the blob URL."""
        blob = self._service.get_blob_client(container=container, blob=path)
        blob.upload_blob(data, overwrite=True, content_settings=content_settings)
        return self.blob_url(container, path)

    def _split_uri(self, uri: str) -> tuple[str, str]:
        """Split a blob https URL into (container, blob_path)."""
        parsed = urlparse(uri)
        parts = parsed.path.lstrip("/").split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"Not a valid blob URL: {uri!r}")
        return parts[0], parts[1]
