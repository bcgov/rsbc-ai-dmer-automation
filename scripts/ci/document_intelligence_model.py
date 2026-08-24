#!/usr/bin/env python3
"""Build or copy a Document Intelligence custom model.

This is a DATA-PLANE operation and is deliberately NOT part of the Bicep
templates in infrastructure/bicep/ — Bicep (an ARM/control-plane tool) can
create the Document Intelligence *account*, but it has no concept of a
custom model, a labeling project, or training data; those live inside the
account's own REST API surface (documentintelligence/*), not as ARM
resources. See docs/deployment/deployment-guide.md for the full
control-plane-vs-data-plane breakdown.

Because the Document Intelligence account has public network access
disabled (see infrastructure/bicep/modules/ai/document-intelligence.bicep),
this script must run somewhere with network line-of-sight to the account's
private endpoint: inside the VNet (e.g. a self-hosted CI runner or an
Azure Automation/Container App job deployed into f11861-dev-vwan-spoke or
its test/prod equivalent), over the platform team's VPN/ExpressRoute, or
via a jump box + Bastion. It does NOT need — and should not use — the ad
hoc Firefox tunnelling workaround; that was only a workaround for a
*browser* (Document Intelligence Studio) that had no other route to the
private endpoint. A script has more options: run it from a host that is
actually inside/routed to the VNet.

Authentication is via DefaultAzureCredential (Azure AD), matching the
"Managed Identity everywhere" rule in docs/standards/security-guidelines.md
— no subscription key is read or accepted here. The identity used needs
the "Cognitive Services User" role on the target Document Intelligence
account (granted in infrastructure/bicep/main.bicep for
id-rsbc-dmer-di-processor-<env>-001; use `az login` / a federated OIDC
identity with the same role for interactive/CI use). For `copy`, the
identity needs that role on BOTH the source and target accounts — Bicep
only grants it on the account in the environment it deploys, so if you run
`copy` interactively you may need it granted on the other account too.

Training data must already exist as labelled documents (fields.json,
<doc>.ocr.json, <doc>.labels.json) in the `raw` blob container — produced
via Document Intelligence Studio's labeling UI (see module docstring in
infrastructure/bicep/modules/ai/document-intelligence.bicep for why that
step can't be automated). This script only triggers the *build* (training)
and, optionally, *copy* (promotion to another environment's account) calls
against data that's already labelled.

Idempotency: both subcommands check whether a model with the given
--model-id already exists (on the target account, for `copy`) before doing
anything. If it does, the command is skipped rather than re-run — Document
Intelligence model IDs are immutable once built, so re-running
`begin_build_document_model`/`begin_copy_model_to` against an existing
model ID fails with a 409 Conflict rather than updating it in place. Pass
--force to delete the existing model first and recreate it under the same
ID (useful while iterating on a model during development); omit it for a
safe, idempotent "create if not exists" run, e.g. from a CI pipeline that
might retry.

`copy` can optionally also carry the labelled training data (the source
documents plus their fields.json/<doc>.ocr.json/<doc>.labels.json files)
from the source account's blob container to the target account's, via
--copy-training-data --source-container-url ... --target-container-url ...
This is a separate, additive step from the model copy itself (the trained
model works without it — Document Intelligence bakes the model from the
training data at build time and doesn't need the source documents again
for inference) and exists so a later retrain/relabel against the target
account has the same starting data available. It is a DATA-PLANE blob
operation using azure-storage-blob (a new dependency for this script —
add it wherever azure-ai-documentintelligence/azure-identity are already
pinned for this project), authenticated the same way as everything else
here (DefaultAzureCredential, no SAS/keys). It needs Storage Blob Data
Reader on the source account and Storage Blob Data Contributor on the
target account for whichever identity runs it — Bicep only grants Reader
on an environment's own storage account to that environment's di-processor
identity, so a cross-account/cross-environment copy needs those roles
granted to the running identity separately; see
docs/deployment/deployment-guide.md. It also needs network line-of-sight
to BOTH storage accounts' blob private endpoints, same as the rest of this
script needs it for the Document Intelligence account. Like the model
copy, existing blobs at the target are skipped unless --force is set
(compared by name only, not content — pass --force to overwrite blobs
that may have changed at the source). This does a straightforward
download/upload per blob via the SDK, appropriate for typical labelled
training-set sizes (tens to low hundreds of documents); for a very large
data set, a dedicated bulk-copy tool such as AzCopy would be more
efficient.

Usage:
    # Build a model in-place (e.g. DEV) from labelled data in a container:
    python document_intelligence_model.py build \\
        --endpoint https://di-rsbc-dmer-shared-dev-001.cognitiveservices.azure.com/ \\
        --container-url https://stdmerdevcac001.blob.core.windows.net/raw \\
        --model-id dmer-v1

    # Copy a trained model from one environment's account to another
    # (the recommended way to promote a model dev -> test -> prod, rather
    # than retraining per environment):
    python document_intelligence_model.py copy \\
        --endpoint https://di-rsbc-dmer-shared-dev-001.cognitiveservices.azure.com/ \\
        --target-endpoint https://di-rsbc-dmer-shared-test-001.cognitiveservices.azure.com/ \\
        --model-id dmer-v1

    # Force-rebuild/re-copy over an existing model ID:
    python document_intelligence_model.py build ... --model-id dmer-v1 --force

    # Copy a trained model AND carry its labelled training data over to the
    # target account's container (e.g. cutting DEV over from the old
    # manually-created account to the new Bicep-managed one):
    python document_intelligence_model.py copy \\
        --endpoint https://rsbc-dmer-ai-optimization.cognitiveservices.azure.com/ \\
        --target-endpoint https://di-rsbc-dmer-shared-dev-001.cognitiveservices.azure.com/ \\
        --model-id dmer-v1 \\
        --copy-training-data \\
        --source-container-url https://rsbcstorage.blob.core.windows.net/raw \\
        --target-container-url https://stdmerdevcac001.blob.core.windows.net/raw
"""

from __future__ import annotations

import argparse
import sys

from azure.ai.documentintelligence import DocumentIntelligenceAdministrationClient
from azure.ai.documentintelligence.models import (
    AuthorizeCopyRequest,
    AzureBlobContentSource,
    BuildDocumentModelRequest,
    DocumentBuildMode,
    DocumentModelDetails,
)
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import ContainerClient


def get_existing_model(
    client: DocumentIntelligenceAdministrationClient, model_id: str
) -> DocumentModelDetails | None:
    """Return the model's details if model_id already exists, else None."""
    try:
        return client.get_model(model_id)
    except ResourceNotFoundError:
        return None


def build_model(
    endpoint: str,
    container_url: str,
    model_id: str,
    description: str | None,
    prefix: str | None,
    force: bool,
) -> None:
    client = DocumentIntelligenceAdministrationClient(
        endpoint, DefaultAzureCredential()
    )

    existing = get_existing_model(client, model_id)
    if existing is not None:
        if not force:
            print(
                f"Model '{model_id}' already exists on {endpoint} "
                f"(created={existing.created_date_time}, "
                f"expires={existing.expiration_date_time}). Skipping build — "
                "model IDs are immutable once built. Pass --force to delete "
                "and rebuild under the same ID, or use a different "
                "--model-id to build a new version alongside it."
            )
            return
        print(
            f"--force set: deleting existing model '{model_id}' on {endpoint} "
            "before rebuilding."
        )
        client.delete_model(model_id)

    blob_source = AzureBlobContentSource(container_url=container_url, prefix=prefix)
    poller = client.begin_build_document_model(
        BuildDocumentModelRequest(
            model_id=model_id,
            build_mode=DocumentBuildMode.TEMPLATE,
            azure_blob_source=blob_source,
            description=description,
        )
    )
    model = poller.result()
    print(
        f"Built model_id={model.model_id} created={model.created_date_time} "
        f"expires={model.expiration_date_time}"
    )


def copy_training_data(
    source_container_url: str,
    target_container_url: str,
    prefix: str | None,
    force: bool,
    credential: DefaultAzureCredential,
) -> None:
    """Copy every blob (documents + fields.json/.ocr.json/.labels.json) from
    the source container to the target container, skipping blobs that
    already exist at the target unless force is set."""
    source_container = ContainerClient.from_container_url(
        source_container_url, credential=credential
    )
    target_container = ContainerClient.from_container_url(
        target_container_url, credential=credential
    )

    copied = 0
    skipped = 0
    for blob in source_container.list_blobs(name_starts_with=prefix):
        target_blob = target_container.get_blob_client(blob.name)
        if not force and target_blob.exists():
            skipped += 1
            continue
        source_blob = source_container.get_blob_client(blob.name)
        data = source_blob.download_blob(max_concurrency=4).readall()
        target_blob.upload_blob(data, overwrite=force)
        copied += 1

    print(
        f"Training data copy: {copied} blob(s) copied from "
        f"{source_container_url} to {target_container_url}"
        + (
            f", {skipped} already present at the target and skipped "
            "(pass --force to overwrite)."
            if skipped
            else "."
        )
    )
    if copied == 0 and skipped == 0:
        print(
            "Warning: no blobs found under the given source container/prefix — "
            "double check --source-container-url and --training-data-prefix."
        )


def copy_model(
    endpoint: str,
    target_endpoint: str,
    model_id: str,
    force: bool,
    copy_training_data_flag: bool,
    source_container_url: str | None,
    target_container_url: str | None,
    training_data_prefix: str | None,
) -> None:
    credential = DefaultAzureCredential()
    source_client = DocumentIntelligenceAdministrationClient(endpoint, credential)
    target_client = DocumentIntelligenceAdministrationClient(
        target_endpoint, credential
    )

    existing = get_existing_model(target_client, model_id)
    skip_model_copy = existing is not None and not force
    if skip_model_copy:
        print(
            f"Model '{model_id}' already exists on target {target_endpoint} "
            f"(created={existing.created_date_time}, "
            f"expires={existing.expiration_date_time}). Skipping model copy — "
            "pass --force to delete the existing target model and copy again. "
            "(Training data, if --copy-training-data is set, is still copied "
            "below — that step has its own independent skip-if-exists check.)"
        )
    else:
        if existing is not None:
            print(
                f"--force set: deleting existing model '{model_id}' on target "
                f"{target_endpoint} before copying."
            )
            target_client.delete_model(model_id)
        authorization = target_client.authorize_model_copy(
            AuthorizeCopyRequest(model_id=model_id)
        )
        poller = source_client.begin_copy_model_to(
            model_id=model_id, body=authorization
        )
        result = poller.result()
        print(f"Copied model_id={result.model_id} to {target_endpoint}")

    if copy_training_data_flag:
        copy_training_data(
            source_container_url,
            target_container_url,
            training_data_prefix,
            force,
            credential,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser(
        "build", help="Train a new custom model from labelled data in blob storage."
    )
    build_parser.add_argument(
        "--endpoint", required=True, help="Document Intelligence account endpoint."
    )
    build_parser.add_argument(
        "--container-url",
        required=True,
        help="https://<account>.blob.core.windows.net/<container> holding labelled training data.",
    )
    build_parser.add_argument(
        "--model-id", required=True, help="Unique model identifier, e.g. dmer-v1."
    )
    build_parser.add_argument("--description", default=None)
    build_parser.add_argument(
        "--prefix",
        default=None,
        help="Optional blob path prefix, if training data isn't at the container root.",
    )
    build_parser.add_argument(
        "--force",
        action="store_true",
        help="Delete an existing model with the same --model-id before rebuilding, "
        "instead of skipping the build.",
    )

    copy_parser = subparsers.add_parser(
        "copy",
        help="Copy a trained model from one Document Intelligence account to another (e.g. dev -> test -> prod).",
    )
    copy_parser.add_argument(
        "--endpoint",
        required=True,
        help="Source Document Intelligence account endpoint.",
    )
    copy_parser.add_argument(
        "--target-endpoint",
        required=True,
        help="Target Document Intelligence account endpoint.",
    )
    copy_parser.add_argument(
        "--model-id",
        required=True,
        help="Model ID to copy (must already exist on the source account).",
    )
    copy_parser.add_argument(
        "--force",
        action="store_true",
        help="Delete an existing model with the same --model-id on the target account "
        "before copying, instead of skipping the copy. Also applies to "
        "--copy-training-data: overwrites blobs that already exist at the target "
        "instead of skipping them.",
    )
    copy_parser.add_argument(
        "--copy-training-data",
        action="store_true",
        help="Also copy the labelled training data (documents + fields.json/"
        ".ocr.json/.labels.json) from --source-container-url to "
        "--target-container-url. Requires both of those to be set.",
    )
    copy_parser.add_argument(
        "--source-container-url",
        default=None,
        help="https://<account>.blob.core.windows.net/<container> holding the "
        "source labelled training data. Required with --copy-training-data.",
    )
    copy_parser.add_argument(
        "--target-container-url",
        default=None,
        help="https://<account>.blob.core.windows.net/<container> to copy the "
        "labelled training data into. Required with --copy-training-data.",
    )
    copy_parser.add_argument(
        "--training-data-prefix",
        default=None,
        help="Optional blob path prefix, to copy only a subset of the source "
        "container. Only used with --copy-training-data.",
    )

    args = parser.parse_args(argv)

    if args.command == "build":
        build_model(
            args.endpoint,
            args.container_url,
            args.model_id,
            args.description,
            args.prefix,
            args.force,
        )
    elif args.command == "copy":
        if args.copy_training_data and not (
            args.source_container_url and args.target_container_url
        ):
            parser.error(
                "--copy-training-data requires both --source-container-url and "
                "--target-container-url."
            )
        copy_model(
            args.endpoint,
            args.target_endpoint,
            args.model_id,
            args.force,
            args.copy_training_data,
            args.source_container_url,
            args.target_container_url,
            args.training_data_prefix,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
