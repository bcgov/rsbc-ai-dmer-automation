#!/bin/bash
#
# Lints changed .bicep files with the Azure CLI Bicep extension.
# Requires `az` with the `bicep` extension (az bicep install), which is
# already a documented prerequisite in docs/development/local-development.md.

set -euo pipefail

if ! command -v az >/dev/null 2>&1; then
  echo "az CLI not found — install it and run 'az bicep install' before committing Bicep changes." >&2
  exit 1
fi

status=0
for f in "$@"; do
  echo "bicep lint: $f"
  az bicep lint --file "$f" || status=1
done

exit "$status"
