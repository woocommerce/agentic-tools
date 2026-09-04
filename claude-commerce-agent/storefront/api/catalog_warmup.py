# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""Pre-fill the product grid at startup.

The web app's grid is drawn from the backend's display cache, which a fresh host has not
filled yet, so the first visitor to a new deployment would see an empty page. This module
asks the Store API for the shop's best sellers once and hands each one to
``warm_display_cache``. Only the display cache changes: no session gains provenance, so a
warmed product still has to be read in conversation before it can go into that session's
cart. Any failure is logged once and the grid fills from sessions instead.
``CATALOG_WARMUP=0`` skips the step.
"""

from __future__ import annotations

import logging
import os

from .store_client import STORE_V1
from .woo_backend import WooStorefrontBackend, product_details_from

logger = logging.getLogger(__name__)

GRID_SIZE = 24


def preload_enabled() -> bool:
    return os.environ.get("CATALOG_WARMUP", "1") != "0"


async def preload_grid(backend: WooStorefrontBackend, limit: int = GRID_SIZE) -> int:
    """Cache the store's ``limit`` most popular products for the grid; returns how many
    were cached, zero on any failure."""
    if not preload_enabled():
        return 0
    query = {"per_page": limit, "orderby": "popularity", "order": "desc"}
    try:
        body, _ = await backend.client.get(f"{STORE_V1}/products", query)
        records = (
            [r for r in body if isinstance(r, dict) and r.get("id")]
            if isinstance(body, list)
            else []
        )
        if not records:
            raise LookupError("the Store API listed no products")
        for record in records:
            # A listing carries no variation rows; keep any a session already fetched.
            known = backend.product(str(record["id"]))
            variants = known.variants if known else []
            backend.warm_display_cache(product_details_from(record, variants))
    except Exception as failure:  # noqa: BLE001 - the grid is a convenience, never a blocker
        logger.warning("grid pre-fill skipped: %s", failure)
        return 0
    logger.info("grid pre-filled with %d products from %s", len(records), backend.client.store_url)
    return len(records)
