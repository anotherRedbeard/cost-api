#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

source scripts/common.sh

require_cmd az
require_cmd azd

select_or_create_azd_env "${1:-${AZD_ENV_NAME:-}}"

: "${AZURE_SUBSCRIPTION_ID:?Set AZURE_SUBSCRIPTION_ID before running this script.}"
: "${AZURE_LOCATION:?Set AZURE_LOCATION before running this script.}"

azd env set AZURE_SUBSCRIPTION_ID "$AZURE_SUBSCRIPTION_ID" >/dev/null
azd env set AZURE_LOCATION "$AZURE_LOCATION" >/dev/null

if [ -n "${COST_QUERY_TIMEFRAME:-}" ]; then
  azd env set COST_QUERY_TIMEFRAME "$COST_QUERY_TIMEFRAME" >/dev/null
fi

if [ -n "${COST_QUERY_GRANULARITY:-}" ]; then
  azd env set COST_QUERY_GRANULARITY "$COST_QUERY_GRANULARITY" >/dev/null
fi

echo "Provisioning infrastructure with azd for environment: $AZD_ENV_NAME"
azd provision --no-prompt

load_azd_env
require_azd_value AZURE_RESOURCE_GROUP
require_azd_value AZURE_FUNCTION_APP_NAME

echo "Provisioned resource group: $AZURE_RESOURCE_GROUP"
echo "Provisioned Function App: $AZURE_FUNCTION_APP_NAME"
