#!/bin/bash
# apply_roles.sh
#
# Runs create-principal.sql then roles.sql (this same directory) against
# Postgres for one environment, for di-processor's Managed Identity. Mirrors
# services/intake-processor/apply_roles.sh.
#
# The identity name is derived from deployment/<env>/parameters.json (the
# file main.bicep deploys with) using the same naming rule main.bicep uses
# (modules/shared/naming.bicep): id-rsbc-dmer-di-processor-<environment>-<instance>.
#
# Prerequisites:
#   * you are a Microsoft Entra administrator on the target server (see
#     create-principal.sql's header);
#   * the Flyway migrations V0001-V0003 are applied to `dmer`;
#   * psql and az are installed, and `az login` is done. From a laptop the
#     server is private: run this on the jump box.
#
# Not run by any pipeline -- apply by hand once per environment.
#
# Usage:
#   ./apply_roles.sh <env> <postgres-host> <your-entra-email>
#
# Example:
#   ./apply_roles.sh dev psql-rsbc-dmer-shared-dev-001.postgres.database.azure.com someone@gov.bc.ca
set -euo pipefail

USAGE="usage: apply_roles.sh <env> <postgres-host> <your-entra-email>"
ENV="${1:?${USAGE}}"
POSTGRES_HOST="${2:?${USAGE}}"
ENTRA_EMAIL="${3:?${USAGE}}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PARAMETERS_FILE="${REPO_ROOT}/deployment/${ENV}/parameters.json"

if [ ! -f "${PARAMETERS_FILE}" ]; then
  echo "No parameters file found at ${PARAMETERS_FILE}" >&2
  exit 1
fi

IDENTITY_NAME=$(python3 -c "
import json
p = json.load(open('${PARAMETERS_FILE}'))['parameters']
print('id-rsbc-dmer-di-processor-{}-{}'.format(p['environment']['value'], p['instance']['value']))
")

echo "Applying Postgres roles for '${IDENTITY_NAME}' (${ENV}) on ${POSTGRES_HOST}..."

AAD_TOKEN=$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)

echo "Step 1/2: creating Entra principal (against 'postgres' database)..."
PGPASSWORD="${AAD_TOKEN}" \
  psql "host=${POSTGRES_HOST} port=5432 dbname=postgres user=${ENTRA_EMAIL} sslmode=require" \
  -v ON_ERROR_STOP=1 \
  -v identity_name="${IDENTITY_NAME}" \
  -f "${SCRIPT_DIR}/create-principal.sql"

echo "Step 2/2: granting table privileges (against 'dmer' database)..."
PGPASSWORD="${AAD_TOKEN}" \
  psql "host=${POSTGRES_HOST} port=5432 dbname=dmer user=${ENTRA_EMAIL} sslmode=require" \
  -v ON_ERROR_STOP=1 \
  -v identity_name="${IDENTITY_NAME}" \
  -f "${SCRIPT_DIR}/roles.sql"

echo "Done."
