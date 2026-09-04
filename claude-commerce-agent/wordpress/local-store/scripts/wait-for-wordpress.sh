#!/usr/bin/env bash
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_env
require_cmd curl
url="${WORDPRESS_PUBLIC_URL:-http://localhost:8090}"
deadline="${WORDPRESS_WAIT_SECONDS:-180}"
start="$(date +%s)"
echo "Waiting for WordPress at ${url}"
while true; do
  if curl -fsS "${url}/wp-admin/install.php" >/dev/null 2>&1 || curl -fsS "${url}/wp-json" >/dev/null 2>&1; then
    echo "WordPress is responding."; exit 0
  fi
  if [ "$(( $(date +%s) - start ))" -gt "$deadline" ]; then
    echo "Timed out waiting for WordPress at ${url}" >&2; exit 1
  fi
  sleep 3
done
