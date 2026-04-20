#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

source scripts/common.sh

require_cmd az
require_cmd curl

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  if command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    require_cmd "$PYTHON_BIN"
  fi
fi

if [ -z "${AZURE_RESOURCE_GROUP:-}" ] || [ -z "${AZURE_FUNCTION_APP_NAME:-}" ]; then
  require_cmd azd
  select_or_create_azd_env "${1:-${AZD_ENV_NAME:-}}"
  load_azd_env
fi

require_azd_value AZURE_RESOURCE_GROUP
require_azd_value AZURE_FUNCTION_APP_NAME

urlencode() {
  "$PYTHON_BIN" -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$1"
}

report_key="$(az functionapp function keys list \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$AZURE_FUNCTION_APP_NAME" \
  --function-name RunEmailCostReport \
  --query default \
  -o tsv)"

base_url="https://${AZURE_FUNCTION_APP_NAME}.azurewebsites.net/api"
report_url="${base_url}/reports/email/run?code=$(urlencode "$report_key")"

report_status="$(curl -sS -o /tmp/cost-api-report.json -w '%{http_code}' "$report_url")"
if [ "$report_status" != "200" ]; then
  echo "Email cost report run failed with status $report_status" >&2
  cat /tmp/cost-api-report.json >&2
  exit 1
fi

echo "Email cost report response:"
cat /tmp/cost-api-report.json
echo
