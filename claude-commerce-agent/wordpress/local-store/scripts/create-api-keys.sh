#!/usr/bin/env bash
# Point merchant/.env at the local store, minting a REST API key if it needs one. A key
# already in the file that still authenticates is kept: WooCommerce stores only a hash, so
# minting a replacement would invalidate the copy a running merchant host read at startup.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_env
require_cmd docker
require_cmd curl

store_url="${WORDPRESS_PUBLIC_URL:-http://localhost:8090}"
env_file="${REPO_ROOT}/merchant/.env"
[ -f "$env_file" ] || cp "${REPO_ROOT}/merchant/.env.example" "$env_file"

# WooCommerce accepts the key and secret as query parameters over plain HTTP, which is what
# the local site is served over; Basic auth would need TLS. Redirects are deliberately not
# followed: the credentials ride in the query string, so a redirect would hand them to
# whatever origin it names. Only 200 counts as proof -- a 3xx never reached the check.
credentials_work() {
  local key="$1" secret="$2" code
  [ -n "$key" ] && [ -n "$secret" ] || return 1
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
    "${store_url}/wp-json/wc/v3/products?per_page=1&consumer_key=${key}&consumer_secret=${secret}" \
    2>/dev/null)" || code="000"
  [ "$code" = "200" ]
}

key="$(get_env_value WOOCOMMERCE_CONSUMER_KEY "$env_file")"
secret="$(get_env_value WOOCOMMERCE_CONSUMER_SECRET "$env_file")"
if credentials_work "$key" "$secret"; then
  echo "Kept the REST API key in merchant/.env; it still authenticates against the store."
else
  json="$(wp eval-file /workspace/scripts/create-api-keys.php | tail -n 1)"
  key="$(printf '%s' "$json" | sed -n 's/.*"consumer_key":"\([^"]*\)".*/\1/p')"
  secret="$(printf '%s' "$json" | sed -n 's/.*"consumer_secret":"\([^"]*\)".*/\1/p')"
  if [ -z "$key" ] || [ -z "$secret" ]; then
    # The payload carries the key and secret, so it is reported with both taken out.
    redacted="$(printf '%s' "$json" | sed -e 's/"consumer_key":"[^"]*"/"consumer_key":"[redacted]"/' -e 's/"consumer_secret":"[^"]*"/"consumer_secret":"[redacted]"/')"
    echo "Failed to generate WooCommerce API credentials: $redacted" >&2; exit 1
  fi
  set_env_value WOOCOMMERCE_CONSUMER_KEY "$key" "$env_file"
  set_env_value WOOCOMMERCE_CONSUMER_SECRET "$secret" "$env_file"
  echo "Wrote a local-only REST API key to merchant/.env."
fi
set_env_value WOOCOMMERCE_LOCAL_STORE "0" "$env_file"
set_env_value WOOCOMMERCE_STORE_URL "$store_url" "$env_file"
