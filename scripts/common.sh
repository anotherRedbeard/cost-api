#!/usr/bin/env bash

set -euo pipefail

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

trim_wrapped_quotes() {
  local value="${1:-}"
  value="${value#\"}"
  value="${value%\"}"
  printf '%s' "$value"
}

select_or_create_azd_env() {
  local env_name="${1:-${AZD_ENV_NAME:-}}"

  if [ -z "$env_name" ]; then
    echo "Set AZD_ENV_NAME or pass the azd environment name as the first argument." >&2
    exit 1
  fi

  export AZD_ENV_NAME="$env_name"

  if [ -f ".azure/${env_name}/.env" ]; then
    azd env select "$env_name" >/dev/null
  else
    azd env new "$env_name" --no-prompt >/dev/null
  fi
}

load_azd_env() {
  while IFS='=' read -r key value; do
    if [ -z "${key:-}" ]; then
      continue
    fi

    value="$(trim_wrapped_quotes "${value:-}")"
    export "${key}=${value}"
  done < <(azd env get-values)
}

require_azd_value() {
  local name="$1"
  if [ -z "${!name:-}" ]; then
    echo "Missing required azd environment value: $name" >&2
    exit 1
  fi
}
