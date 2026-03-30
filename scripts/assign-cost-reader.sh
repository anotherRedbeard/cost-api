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
require_azd_value COST_SUBSCRIPTION_ID

principal_id="${FUNCTION_APP_PRINCIPAL_ID:-}"

if [ -z "$principal_id" ]; then
  principal_id="$(az functionapp identity show \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$AZURE_FUNCTION_APP_NAME" \
    --query principalId \
    -o tsv)"
fi

if [ -z "$principal_id" ]; then
  echo "Unable to resolve the Function App managed identity principal ID." >&2
  exit 1
fi

scope="/subscriptions/${COST_SUBSCRIPTION_ID}"
assignment_count="$(az role assignment list \
  --assignee-object-id "$principal_id" \
  --scope "$scope" \
  --query "[?roleDefinitionName=='Cost Management Reader'] | length(@)" \
  -o tsv)"

if [ "$assignment_count" = "0" ]; then
  az role assignment create \
    --assignee-object-id "$principal_id" \
    --assignee-principal-type ServicePrincipal \
    --role "Cost Management Reader" \
    --scope "$scope" \
    >/dev/null
  echo "Assigned Cost Management Reader to $principal_id on $scope"
else
  echo "Cost Management Reader already assigned to $principal_id on $scope"
fi
