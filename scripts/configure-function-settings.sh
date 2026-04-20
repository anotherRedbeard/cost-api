#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

source scripts/common.sh

require_cmd az
require_cmd azd

select_or_create_azd_env "${1:-${AZD_ENV_NAME:-}}"
load_azd_env

require_azd_value AZURE_RESOURCE_GROUP
require_azd_value AZURE_FUNCTION_APP_NAME

: "${TENANT_ID:?Set TENANT_ID before running this script.}"
: "${CLIENT_ID:?Set CLIENT_ID before running this script.}"
: "${CLIENT_SECRET:?Set CLIENT_SECRET before running this script.}"
MONTHLY_REPORT_SCHEDULE="${MONTHLY_REPORT_SCHEDULE:-0 0 14 * * 1-5}"

echo "Configuring runtime app settings for $AZURE_FUNCTION_APP_NAME"

az functionapp config appsettings set \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$AZURE_FUNCTION_APP_NAME" \
  --settings \
    "TENANT_ID=${TENANT_ID}" \
    "CLIENT_ID=${CLIENT_ID}" \
    "CLIENT_SECRET=${CLIENT_SECRET}" \
    "MONTHLY_REPORT_SCHEDULE=${MONTHLY_REPORT_SCHEDULE}" \
  --output none

echo "Runtime settings configured successfully"
