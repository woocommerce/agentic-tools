#!/usr/bin/env bash
# Shared helpers for the local-store scripts. Copyright 2026 Automattic Inc. Apache-2.0.
set -euo pipefail

# Resolved while this file is sourced: BASH_SOURCE is relative to the directory the script
# was invoked from, so it stops resolving the moment anything changes directory.
STORE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "${STORE_DIR}/../.." && pwd)"

load_env() {
  local env_file="${STORE_DIR}/.env"
  if [ ! -f "$env_file" ]; then
    cp "${STORE_DIR}/.env.example" "$env_file"
    echo "Created wordpress/local-store/.env from .env.example"
  fi
  set -a
  # shellcheck disable=SC1091
  . "$env_file"
  set +a
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing required command: $1" >&2; exit 127; }
}

compose() { docker compose -f "${STORE_DIR}/docker-compose.yml" "$@"; }
# Each wp call is its own `compose run`; quiet progress keeps Compose from printing the
# dependency containers' status before every command. `compose` itself stays verbose so a
# first-run image pull is visible.
wp() { docker compose --progress quiet -f "${STORE_DIR}/docker-compose.yml" run --rm -T wpcli "$@"; }

get_env_value() {
  local key="$1" file="$2"
  [ -f "$file" ] || return 0
  sed -n "s/^${key}=//p" "$file" | tail -n 1
}

set_env_value() {
  local key="$1" value="$2" file="$3" tmp
  touch "$file"
  tmp="$(mktemp)"
  awk -v key="$key" -v value="$value" '
    BEGIN { done = 0 }
    $0 ~ "^#? ?" key "=" { print key "=" value; done = 1; next }
    { print }
    END { if (!done) print key "=" value }
  ' "$file" > "$tmp"
  mv "$tmp" "$file"
}
