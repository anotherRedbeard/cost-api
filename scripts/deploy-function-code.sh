#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

source scripts/common.sh

require_cmd az
require_cmd azd

select_or_create_azd_env "${1:-${AZD_ENV_NAME:-}}"
load_azd_env

require_azd_value AZURE_SUBSCRIPTION_ID
require_azd_value AZURE_RESOURCE_GROUP
require_azd_value AZURE_FUNCTION_APP_NAME

zip_path="${FUNCTION_ZIP_PATH:-dist/functionapp.zip}"

if [ ! -f "$zip_path" ]; then
  echo "Deployment archive not found at $zip_path. Run scripts/package-function-code.sh first." >&2
  exit 1
fi

echo "Deploying $zip_path to $AZURE_FUNCTION_APP_NAME"
az functionapp deployment source config-zip \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$AZURE_FUNCTION_APP_NAME" \
  --src "$zip_path" \
  --build-remote false \
  --timeout 1800

echo "Syncing function triggers"
az rest \
  --method post \
  --url "https://management.azure.com/subscriptions/${AZURE_SUBSCRIPTION_ID}/resourceGroups/${AZURE_RESOURCE_GROUP}/providers/Microsoft.Web/sites/${AZURE_FUNCTION_APP_NAME}/syncfunctiontriggers?api-version=2022-03-01" \
  >/dev/null

for attempt in $(seq 1 12); do
  functions="$(az functionapp function list \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$AZURE_FUNCTION_APP_NAME" \
    --query "[].name" \
    -o tsv)"

  if [ -n "$functions" ]; then
    echo "Functions registered:"
    printf '%s\n' "$functions"
    exit 0
  fi

  echo "Waiting for functions to register (attempt $attempt/12)"
  sleep 10
done

echo "Deployment completed but Azure still reports zero functions." >&2
exit 1
