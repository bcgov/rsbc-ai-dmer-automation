"""Normalize Durable activity: artifact I/O, retry recovery and stage auditing.

See docs/development/stages/04-activity-normalize.md. The shared library owns
clinical normalization; a database session serializes attempts per document.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import azure.durable_functions as df
from azure.identity import DefaultAzureCredential
from dmer_common.config import openai_settings
from dmer_common.db.normalization import (
    NormalizationBusyError,
    NormalizationNotReadyError,
    NormalizationStateError,
    normalization_session,
)
from dmer_common.normalization import (
    NORMALIZATION_SCHEMA_VERSION,
    NormalizationValidationError,
    normalize_document,
)
from dmer_common.openai_client import OpenAIClient
from dmer_common.storage import BlobClient, normalized_dmer, normalized_path
from dmer_common.telemetry import document_id_context, get_logger
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

_log = get_logger(__name__)

_AAD_POSTGRES_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"

ACTIVITY_NAME = "NormalizeDmer"


class NormalizationActivityError(RuntimeError):
    """Operational failure; the document orchestrator owns bounded retries."""


def _input(payload: dict) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise NormalizationValidationError("Normalize input must be an object")
    document_id = payload.get("document_id")
    blob_url = payload.get("extracted_blob_url")
    try:
        if not isinstance(document_id, str):
            raise TypeError
        document_id = str(UUID(document_id))
    except (ValueError, TypeError):
        raise NormalizationValidationError("Normalize input requires a document UUID") from None
    if not isinstance(blob_url, str):
        raise NormalizationValidationError("Normalize input requires an extracted blob URL")
    parsed = urlparse(blob_url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc or not parsed.path.strip("/"):
        raise NormalizationValidationError("Normalize input requires an extracted blob URL")
    return document_id, blob_url


def _extracted_fields(content: bytes) -> dict:
    """Accept the existing artifact and the revised named-section layout."""
    try:
        extraction = json.loads(content)
        if not isinstance(extraction, dict):
            raise TypeError
        combined = extraction.get("combined", extraction)
        if not isinstance(combined, dict) or not isinstance(combined.get("fields"), dict):
            raise TypeError
        return combined["fields"]
    except (ValueError, UnicodeError, TypeError):
        raise NormalizationValidationError("Invalid combined extraction artifact") from None


async def _run_sync(fn, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking call in a thread so it doesn't stall the event loop
    other concurrent invocations of this Functions host share -- same
    convention as intake-processor's function_app.py (a shared-host
    Functions app; unlike di-processor's dedicated-consumer Container App,
    where a blocking call in an async method doesn't carry the same
    other-invocations-blocked risk).
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


async def _get_async_engine() -> AsyncEngine:
    """Build a fresh async Postgres engine for this invocation.

    Duplicated from intake-processor's own ``_get_async_engine`` (same AAD/
    Managed Identity flow, same NullPool "no pooling across invocations"
    rationale) rather than shared, because dmer_common doesn't yet have a
    single canonical engine-builder helper -- worth factoring out once a
    third service needs the exact same code, not before.
    """
    host = os.environ["POSTGRES_HOST"]
    port = int(os.environ.get("POSTGRES_PORT", "5432"))
    database = os.environ.get("POSTGRES_DATABASE", "dmer")
    user = os.environ["POSTGRES_USER"]
    password = os.environ.get("POSTGRES_PASSWORD")
    sslmode = os.environ.get("POSTGRES_SSLMODE", "require")

    if not password:
        credential = DefaultAzureCredential()
        try:
            token = await _run_sync(credential.get_token, _AAD_POSTGRES_SCOPE)
            password = token.token
        finally:
            await _run_sync(credential.close)

    url = URL.create(
        "postgresql+asyncpg",
        username=user,
        password=password,
        host=host,
        port=port,
        database=database,
    )
    connect_args = {} if sslmode == "disable" else {"ssl": sslmode}
    return create_async_engine(url, poolclass=NullPool, connect_args=connect_args)


async def normalize_dmer_activity(payload: dict) -> dict:
    """Return a persisted output URL, including on a retried invocation.

    Input contains document_id and extracted_blob_url only (other identifiers
    may be carried by the orchestrator). Clinical content stays in blobs.
    Errors are sanitized before reaching Durable history or Application Insights.
    """
    document_id, extracted_blob_url = _input(payload)
    engine = None
    with document_id_context(document_id):
        _log.info("normalize: triggered")
        try:
            settings = openai_settings()
            model_version = f"{settings.deployment}@{NORMALIZATION_SCHEMA_VERSION}"
            engine = await _get_async_engine()
            async with normalization_session(engine, document_id) as repository:
                run = await repository.start_or_resume(
                    now=datetime.now(UTC), model_version=model_version
                )
                if run.output_blob_url:
                    _log.info("normalize: returning completed artifact")
                    return {"normalized_blob_url": run.output_blob_url}
                try:
                    blob = BlobClient(os.environ["BLOB_ACCOUNT_URL"])
                    content = await _run_sync(blob.download, extracted_blob_url)
                    fields = _extracted_fields(content)
                    normalized = await _run_sync(
                        normalize_document, OpenAIClient(settings=settings), fields
                    )
                    output_url = await _run_sync(
                        blob.upload_json,
                        normalized_dmer(),
                        normalized_path(document_id, run_id=run.run_id),
                        normalized,
                    )
                    await repository.succeed(
                        run.run_id, ended_at=datetime.now(UTC),
                        output_blob_url=output_url, model_version=model_version,
                    )
                    _log.info("normalize: succeeded", extra={"run_id": run.run_id})
                    return {"normalized_blob_url": output_url}
                except Exception as exc:
                    try:
                        await repository.fail(
                            run.run_id, ended_at=datetime.now(UTC),
                            error_code=type(exc).__name__,
                            error_detail="Normalization attempt failed; see error_code.",
                        )
                    except Exception:  # noqa: BLE001 - preserve original failure without logging payloads
                        _log.error("normalize: unable to record failed attempt")
                    raise
        except NormalizationValidationError:
            raise NormalizationValidationError("Normalization input or output failed validation") from None
        except (NormalizationBusyError, NormalizationNotReadyError, NormalizationStateError):
            raise
        except Exception as exc:  # noqa: BLE001 - sanitize dependency errors before Durable serialization
            _log.error("normalize: operational failure", extra={"error_code": type(exc).__name__})
            raise NormalizationActivityError("Normalization dependency or persistence failure") from None
        finally:
            if engine is not None:
                try:
                    await engine.dispose()
                except Exception:  # noqa: BLE001 - cleanup must not replace an activity result/error
                    _log.error("normalize: unable to dispose database engine")


def register(app: df.DFApp) -> None:
    """Register the Normalize activity on *app* -- called once from
    function_app.py. Kept as a separate registration step (rather than a
    module-level ``@app.activity_trigger`` decorator) so this module has no
    import-time dependency on a live DFApp instance, which is what lets
    :func:`normalize_dmer_activity` above be unit-tested directly.
    """

    @app.function_name(name=ACTIVITY_NAME)
    @app.activity_trigger(input_name="payload")
    async def _normalize_dmer(payload: dict) -> dict:
        return await normalize_dmer_activity(payload)
