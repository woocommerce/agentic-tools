#!/usr/bin/env bash
# One command: start the Docker site, install WooCommerce and the bridge plugin, point
# merchant/.env at it, and seed the catalog and orders from merchant/data/seed.json. Every
# step converges rather than repeats, so re-running repairs a half-finished run and a site
# set up five times looks like one set up once.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_env
require_cmd docker
"${STORE_DIR}/scripts/install-woocommerce.sh"
"${STORE_DIR}/scripts/create-api-keys.sh"
python="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
[ -x "$python" ] || python="python3"
(cd "$REPO_ROOT" && "$python" merchant/scripts/seed_store.py "$@")
# WooCommerce Analytics imports orders on a schedule; run the queue now so the revenue
# report has figures the moment the merchant example starts.
wp action-scheduler run --batch-size=200 --batches=5 >/dev/null 2>&1 || true
echo
echo "Local WooCommerce store is ready."
echo "  Site:      ${WORDPRESS_PUBLIC_URL:-http://localhost:8090}"
echo "  Admin:     ${WORDPRESS_PUBLIC_URL:-http://localhost:8090}/wp-admin (user ${WORDPRESS_ADMIN_USER:-store_admin}; password kept in wordpress/local-store/.env)"
echo "  Merchant:  merchant/.env now points at it."
