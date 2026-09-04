#!/usr/bin/env bash
# Install WordPress, WooCommerce, and the bridge plugin on the Docker site.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_env
require_cmd docker

compose up -d db wordpress
"${STORE_DIR}/scripts/wait-for-wordpress.sh"

if ! wp core is-installed >/dev/null 2>&1; then
  echo "Installing WordPress."
  wp core install \
    --url="${WORDPRESS_PUBLIC_URL:-http://localhost:8090}" \
    --title="${WORDPRESS_TITLE:-ACME Supply Co.}" \
    --admin_user="${WORDPRESS_ADMIN_USER:-store_admin}" \
    --admin_password="${WORDPRESS_ADMIN_PASSWORD:-store-local-admin-password}" \
    --admin_email="${WORDPRESS_ADMIN_EMAIL:-admin@example.test}" \
    --skip-email
else
  echo "WordPress is already installed."
fi

# The image copies core files into the volume only once, so a site created from an older
# image stays on that WordPress until updated here. The current WooCommerce requires the
# current WordPress.
wp core update
wp core update-db

if ! wp plugin is-installed woocommerce >/dev/null 2>&1; then
  echo "Installing WooCommerce from WordPress.org."
  if [ -n "${WOOCOMMERCE_VERSION:-}" ]; then
    wp plugin install woocommerce --version="${WOOCOMMERCE_VERSION}"
  else
    wp plugin install woocommerce
  fi
fi
wp plugin activate woocommerce
wp plugin activate claude-commerce-bridge
# The image's .htaccess already carries the standard rules, so no --hard regeneration.
wp rewrite structure '/%postname%/'
wp option update blogdescription "${WORDPRESS_TAGLINE:-Tools and storage for the workshop bench}"
wp option update timezone_string "America/Toronto"
wp eval-file /workspace/scripts/configure-woocommerce.php
echo "WooCommerce is installed and configured."
