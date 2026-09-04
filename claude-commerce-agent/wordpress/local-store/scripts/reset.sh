#!/usr/bin/env bash
# Delete the local Docker containers and volumes for this Compose project only.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_env
require_cmd docker
if [ "${1:-}" != "--force" ]; then
  echo "This removes the local store's containers and data. Run again with --force." >&2; exit 1
fi
compose down -v --remove-orphans
echo "Local store removed. Run wordpress/local-store/scripts/setup.sh to recreate it."
