#!/bin/bash
# apply_roles.sh
#
# Runs create-principal.sql then roles.sql (this same directory) against
# Postgres for a given environment, pulling the Function App name from the
# same deployment/<env>/intake-processor.parameters.json Bicep itself reads
# -- one source of truth, rather than retyping the name by hand each time
# (which environment/parameters.json changes would silently drift out of
# sync with).
#
# The two files run against different databases, deliberately -- see
# create-principal.sql's header for why: Azure's pgaadauth_* functions only
# exist in the server's default `postgres` database, not one created
# afterward (e.g. `dmer`, via postgresql-database.bicep).
#
# Prerequisite: you must already be a Microsoft Entra administrator on the
# target Postgres server -- see create-principal.sql's own header for how
# to become one. Not run by any pipeline -- see roles.sql's header for why.
#
# Usage:
#   ./apply_roles.sh <env> <postgres-host> <your-entra-email>
#
# Example:
#   ./apply_roles.sh dev rsbc-ai-database-dev.postgres.database.azure.com nicholas.kan@gov.bc.ca
set -euo pipefail

ENV="${1:?usage: apply_roles.sh <env> <postgres-host> <your-entra-email>}"
POSTGRES_HOST="${2:?usage: apply_roles.sh <env> <postgres-host> <your-entra-email>}"
ENTRA_EMAIL="${3:?usage: apply_roles.sh <env> <postgres-host> <your-entra-email>}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PARAMETERS_FILE="${REPO_ROOT}/deployment/${ENV}/intake-processor.parameters.json"

if [ ! -f "${PARAMETERS_FILE}" ]; then
  echo "No parameters file found at ${PARAMETERS_FILE}" >&2
  exit 1
fi

FUNCTION_APP_NAME=$(python3 -c "
import json
print(json.load(open('${PARAMETERS_FILE}'))['parameters']['functionAppName']['value'])
")

echo "Applying Postgres roles for '${FUNCTION_APP_NAME}' (${ENV}) on ${POSTGRES_HOST}..."

AAD_TOKEN=$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)

echo "Step 1/2: creating AAD principal (against 'postgres' database)..."
PGPASSWORD="${AAD_TOKEN}" \
  psql "host=${POSTGRES_HOST} port=5432 dbname=postgres user=${ENTRA_EMAIL} sslmode=require" \
  -v function_app_name="${FUNCTION_APP_NAME}" \
  -f "${SCRIPT_DIR}/create-principal.sql"

echo "Step 2/2: granting table/sequence privileges (against 'dmer' database)..."
PGPASSWORD="${AAD_TOKEN}" \
  psql "host=${POSTGRES_HOST} port=5432 dbname=dmer user=${ENTRA_EMAIL} sslmode=require" \
  -v function_app_name="${FUNCTION_APP_NAME}" \
  -f "${SCRIPT_DIR}/roles.sql"

echo "Done."
