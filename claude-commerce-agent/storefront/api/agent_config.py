# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""The shopping agent's configuration for a WooCommerce site: what the prompt says about
this catalog, and how ids are recognised (they are not: a post id is a bare integer)."""

from __future__ import annotations

from shopping_agent import ShoppingAgentConfig

ASSISTANT_NAME = "the store assistant"
VOICE = "warm, to the point, and honest about what the shop stocks"

CATALOG_NOTES = (
    "This catalog is a WooCommerce site, read live. A product that comes in several "
    "variations (a size or a color, for example) is a family: get_product_details lists "
    "each purchasable variation under its own id, and the cart holds variation ids, never "
    "the family's. Product ids are plain numbers. Checkout, shipping, and payment take "
    "place on the store's own checkout page, so send the shopper there instead of "
    "estimating delivery dates."
)
ORDERS_WITH_BRIDGE = (
    "When asked about orders, the lookup returns only the orders placed from this "
    "conversation's cart; if the order tools come back empty, say that and suggest the "
    "shopper check the confirmation email the store sent."
)
ORDERS_WITHOUT_BRIDGE = (
    "This store offers no order lookup: if the order tools come back empty, say that "
    "directly and suggest the shopper check the confirmation email the store sent."
)


def shopping_config_for(store_name: str, *, bridge: bool = False) -> ShoppingAgentConfig:
    """Config for one store. ``bridge`` says whether the bridge plugin answered the startup
    probe, which decides what the prompt promises about order lookups."""
    orders = ORDERS_WITH_BRIDGE if bridge else ORDERS_WITHOUT_BRIDGE
    return ShoppingAgentConfig(
        brand_name=store_name,
        assistant_name=ASSISTANT_NAME,
        brand_voice=VOICE,
        domain_search_notes=f"{CATALOG_NOTES} {orders}",
        # No id regex: integers appear in ordinary sentences, so grounding comes from the
        # tool calls alone.
        product_id_patterns=(),
    )
