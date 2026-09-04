# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""Live check of the storefront backend against a running WooCommerce site.

Reads ``WOOCOMMERCE_STORE_URL`` (the local Docker site unless set) and walks a shopper's
path through the Store API: probe, search, a family's details, add, handoff, quantity
change, shipping rates, removal, and a policy search. Each step prints one line; the first
failing step ends the run with exit status 1. No model is involved, so no API key is needed.

    python storefront/scripts/smoke.py
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shopping_agent import ShoppingSessionContext  # noqa: E402
from storefront.api.store_client import StoreApiClient, store_url_from_env  # noqa: E402
from storefront.api.woo_backend import WooStorefrontBackend  # noqa: E402


class StepFailed(Exception):
    pass


def expect(condition: object, message: str) -> None:
    if not condition:
        raise StepFailed(message)


def _redacted(url: str) -> str:
    """A handoff URL with its query values replaced. The cart token travels in the query
    string and is a bearer credential for the cart, so printing it would put a live one in
    whatever captures this script's output."""
    parts = urlsplit(url)
    query = "&".join(f"{name}=<redacted>" for name, _ in parse_qsl(parts.query))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


class Walkthrough:
    """The steps, in order. Each returns the line to print and leaves what the next step
    needs on ``self``."""

    def __init__(self, store_url: str) -> None:
        self.client = StoreApiClient(store_url)
        self.backend = WooStorefrontBackend(self.client)
        self.shopper = ShoppingSessionContext(session_id="smoke", user_id="guest")
        self.item_id = ""

    def steps(self) -> list[tuple[str, Callable[[], Awaitable[str]]]]:
        return [
            ("probe", self.probe),
            ("search", self.search),
            ("details", self.details),
            ("add", self.add),
            ("handoff", self.handoff),
            ("update", self.update),
            ("shipping", self.shipping),
            ("remove", self.remove),
            ("policies", self.policies),
        ]

    async def probe(self) -> str:
        await self.backend.probe()
        bridge = "present" if self.backend.bridge_available else "absent"
        return f"{self.backend.store_name!r}, bridge plugin {bridge}"

    async def search(self) -> str:
        products = await self.backend.search_products(self.shopper, "stool", limit=3)
        expect(products, "nothing found for 'stool'")
        self.family_id = next((p.product_id for p in products if p.options), products[0].product_id)
        first = products[0]
        return f"{len(products)} products, first {first.title!r} at {first.price} {first.currency}"

    async def details(self) -> str:
        details = await self.backend.get_product_details(self.shopper, self.family_id)
        expect(details, f"no details for {self.family_id}")
        self.item_id = details.variants[0].product_id if details.variants else details.product_id
        return f"{len(details.variants)} variations; will add {self.item_id}"

    async def add(self) -> str:
        cart = await self.backend.add_to_cart(self.shopper, self.item_id, 1)
        expect(cart.item_count == 1, f"cart holds {cart.item_count} items after one add")
        return f"added {cart.items[0].title!r}"

    async def handoff(self) -> str:
        cart = await self.backend.get_cart(self.shopper)
        handoffs = await self.backend.checkout_handoff(self.shopper, cart)
        expect(handoffs, "no handoff URL for a one-line cart")
        return _redacted(handoffs[0].url)

    async def update(self) -> str:
        cart = await self.backend.update_cart_item(self.shopper, self.item_id, 2)
        expect(cart.item_count == 2, f"quantity is {cart.item_count} after setting 2")
        return "quantity set to 2"

    async def shipping(self) -> str:
        options = await self.backend.get_fulfillment_options(self.shopper, [self.item_id])
        return f"{len(options)} rates" + (f", first {options[0].eta!r}" if options else "")

    async def remove(self) -> str:
        cart = await self.backend.remove_from_cart(self.shopper, self.item_id)
        expect(not cart.items, "the cart still has lines after the remove")
        return "cart emptied"

    async def policies(self) -> str:
        policies = await self.backend.search_policies(self.shopper, "return policy")
        return f"{len(policies)} pages" + (f", first {policies[0].title!r}" if policies else "")

    async def run(self) -> int:
        try:
            for name, step in self.steps():
                print(f"{name:>9}: {await step()}")
        except StepFailed as failure:
            print(f"FAILED: {failure}", file=sys.stderr)
            return 1
        finally:
            await self.client.aclose()
        print(f"smoke check passed against {self.client.store_url}")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(Walkthrough(store_url_from_env()).run()))
