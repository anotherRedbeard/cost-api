#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

source scripts/common.sh

require_cmd az
require_cmd azd
require_cmd curl
require_cmd python

select_or_create_azd_env "${1:-${AZD_ENV_NAME:-}}"
load_azd_env

require_azd_value AZURE_RESOURCE_GROUP
require_azd_value AZURE_FUNCTION_APP_NAME

urlencode() {
  python -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$1"
}

SMOKE_TEST_SUBSCRIPTION_ID="${SMOKE_TEST_SUBSCRIPTION_ID:-${COST_SUBSCRIPTION_ID:-}}"

health_key="$(az functionapp function keys list \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$AZURE_FUNCTION_APP_NAME" \
  --function-name health \
  --query default \
  -o tsv)"

cost_key="$(az functionapp function keys list \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$AZURE_FUNCTION_APP_NAME" \
  --function-name subscription_cost \
  --query default \
  -o tsv)"

base_url="https://${AZURE_FUNCTION_APP_NAME}.azurewebsites.net/api"
health_url="${base_url}/health?code=$(urlencode "$health_key")"

health_status="$(curl -sS -o /tmp/cost-api-health.json -w '%{http_code}' "$health_url")"
if [ "$health_status" != "200" ]; then
  echo "Health check failed with status $health_status" >&2
  cat /tmp/cost-api-health.json >&2
  exit 1
fi

cost_status="$(curl -sS -o /tmp/cost-api-cost.json -w '%{http_code}' "$cost_url")"
if [ "$cost_status" != "200" ]; then
  echo "Cost query failed with status $cost_status" >&2
  cat /tmp/cost-api-cost.json >&2
  exit 1
fi

echo "Health response:"
cat /tmp/cost-api-health.json
echo

if [ -n "$SMOKE_TEST_SUBSCRIPTION_ID" ]; then
  cost_url="${base_url}/cost/subscription?subscriptionId=${SMOKE_TEST_SUBSCRIPTION_ID}&format=json&code=$(urlencode "$cost_key")"
  cost_status="$(curl -sS -o /tmp/cost-api-cost.json -w '%{http_code}' "$cost_url")"
  if [ "$cost_status" != "200" ]; then
    echo "Cost query failed with status $cost_status" >&2
    cat /tmp/cost-api-cost.json >&2
    exit 1
  fi

  echo "Cost response preview:"
  python - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("/tmp/cost-api-cost.json").read_text())
preview = {
    "subscriptionId": payload.get("subscriptionId"),
    "timeframe": payload.get("timeframe"),
    "currency": payload.get("currency"),
    "totalCost": payload.get("totalCost"),
    "rowCount": payload.get("rowCount"),
}
print(json.dumps(preview, indent=2))
PY
else
  validation_url="${base_url}/cost/subscription?code=$(urlencode "$cost_key")"
  validation_status="$(curl -sS -o /tmp/cost-api-validation.json -w '%{http_code}' "$validation_url")"
  if [ "$validation_status" != "400" ]; then
    echo "Expected a validation error without subscriptionId, got $validation_status" >&2
    cat /tmp/cost-api-validation.json >&2
    exit 1
  fi

  echo "Validation response preview:"
  cat /tmp/cost-api-validation.json
  echo
fi
