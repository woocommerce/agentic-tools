# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""Load the fixture catalog and sample orders from ``data/seed.json`` into a WooCommerce site.

    python merchant/scripts/seed_store.py [--dry-run] [--skip-orders] [--no-images]

A fresh WooCommerce install gives the merchant agent nothing to talk about: no products,
no orders, so no alerts, metrics, or issues. This script gives a real site the same eight
products and handful of orders the test suite runs against, so a demo and the suite tell
the same story.

What it does not do:

- Back-date orders. ``POST /orders`` stamps ``date_created`` with the current time, so a
  site seeded today has all of its revenue in the current window and nothing in the
  comparison window. The agent reports the period it measured, so the figures stay honest;
  ``api/local_store.py`` is where two months of history exist, in memory.
- Contact a payment gateway. Refunds are posted with ``api_refund: false`` and exist only
  so the return-spike alert has something to find.

The whole script is idempotent: running it twice leaves the site as one run does. The
catalog (trash included) is listed first and every slug already present is left alone,
prices and stock included, since an operator may have changed them on purpose. Each order
is stamped with its ``ref`` from the seed file under a private meta key, and a ref already
on the site is not placed again, so re-running does not pile up duplicate revenue.
``--skip-orders`` re-runs the catalog part alone.

Images are sideloaded by WordPress from a placeholder host, which needs the site to have
outbound access. ``--no-images`` skips them, and a product whose image sideload fails is
retried without images. ``--dry-run`` prints the requests and sends none.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
EXAMPLE_DIR = HERE.parents[1]
sys.path.insert(0, str(HERE.parents[2]))

from demo_common import load_demo_env  # noqa: E402
from merchant.api.agent_config import MissingCredentials, load_settings  # noqa: E402
from merchant.api.local_store import IMAGE_HOST, TRASHED_SUFFIX  # noqa: E402
from merchant.api.rest_client import WC_V3, WooApiError, WooExecutor, WooRestClient  # noqa: E402

SEED_PATH = EXAMPLE_DIR / "data" / "seed.json"
COST_META_KEY = "_wc_cog_cost"
# Stamped on every order this script places. WooCommerce has no other way to tell a seeded
# order from one a person made, and without it a second run doubles the store's revenue.
SEED_REF_META_KEY = "_seed_ref"
PAID_STATUSES = frozenset({"processing", "completed", "refunded"})
PAGE_SIZE = 100

load_demo_env(EXAMPLE_DIR)


def load_seed(path: Path = SEED_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class RecordingTransport:
    """The ``--dry-run`` transport. Keeps every request, answers reads with an empty page,
    and hands out invented ids for anything created."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, Any]] = []
        self._ids = itertools.count(900)

    async def request(
        self, method: str, path: str, *, params: dict | None = None, json: Any = None
    ) -> tuple[Any, dict[str, str]]:
        self.requests.append((method, path, json if json is not None else params))
        if method == "GET":
            return [], {"X-WP-TotalPages": "1"}
        return {"id": next(self._ids), **(json or {})}, {}

    def dump(self, echo: Callable[[str], None] = print) -> None:
        for method, path, payload in self.requests:
            echo(f"{method:6} {path} {json.dumps(payload)[:120]}")


@dataclass
class SeedOutcome:
    """Ids on the site after a run: products by slug, variations by SKU as
    ``(parent_id, variation_id)``, the orders created this run, and the seed refs already
    on the site when the run started."""

    products: dict[str, int] = field(default_factory=dict)
    variations: dict[str, tuple[int, int]] = field(default_factory=dict)
    orders: list[int] = field(default_factory=list)
    seeded_refs: set[str] = field(default_factory=set)


def _seed_slug(slug: str) -> str:
    """The slug the seed file would call this product. WordPress renames a post's slug when
    it is trashed, so the fixture's trashed product comes back as ``<slug>__trashed`` and
    would otherwise look absent and be created a second time, colliding on its SKU."""
    return slug[: -len(TRASHED_SUFFIX)] if slug.endswith(TRASHED_SUFFIX) else slug


def _cost_meta(cost: Any) -> list[dict[str, str]]:
    return [{"key": COST_META_KEY, "value": str(cost)}] if cost not in (None, "") else []


class Seeder:
    """Materialises one seed spec on one transport. Works over ``WooRestClient``,
    ``LocalStore``, or ``RecordingTransport``: all it needs is ``request``."""

    def __init__(
        self,
        transport: WooExecutor,
        spec: dict[str, Any],
        *,
        images: bool = True,
        orders: bool = True,
        echo: Callable[[str], None] = print,
    ) -> None:
        self.transport = transport
        self.spec = spec
        self.images = images
        self.orders = orders
        self.echo = echo
        self.outcome = SeedOutcome()

    async def run(self) -> SeedOutcome:
        categories = {
            entry["slug"]: await self._category_id(entry) for entry in self.spec["categories"]
        }
        await self._index_existing()
        if self.orders:
            await self._index_seeded_orders()
        for entry in self.spec["products"]:
            if entry["slug"] in self.outcome.products:
                self.echo(f"kept:    {entry['slug']} (already on the site)")
                continue
            await self._create_product(entry, categories.get(entry.get("category") or ""))
        if self.orders:
            await self._place_orders()
        return self.outcome

    # -- Transport helpers ---------------------------------------------------------------

    async def _get(self, path: str, **params: Any) -> tuple[list[dict[str, Any]], dict[str, str]]:
        body, headers = await self.transport.request("GET", f"{WC_V3}/{path}", params=params)
        return (body if isinstance(body, list) else []), headers

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        created, _ = await self.transport.request("POST", f"{WC_V3}/{path}", json=body)
        return created

    async def _pages(self, path: str, **params: Any) -> AsyncIterator[dict[str, Any]]:
        for page in itertools.count(1):
            rows, headers = await self._get(path, per_page=PAGE_SIZE, page=page, **params)
            for row in rows:
                yield row
            if not rows or page >= int(headers.get("X-WP-TotalPages") or 1):
                return

    # -- Categories and the existing catalog ---------------------------------------------

    async def _category_id(self, entry: dict[str, Any]) -> int:
        terms, _ = await self._get("products/categories", slug=entry["slug"], per_page=1)
        for term in terms:
            if term.get("slug") == entry["slug"]:
                return int(term["id"])
        created = await self._post(
            "products/categories", {"name": entry["name"], "slug": entry["slug"]}
        )
        return int(created["id"])

    async def _index_existing(self) -> None:
        """Record every product already on the site, by slug and variation SKU. WordPress
        leaves trashed posts out of ``status=any``, so the trash is listed separately; the
        fixture keeps one product there on purpose."""
        for status in ("any", "trash"):
            async for node in self._pages("products", status=status):
                slug = _seed_slug(node["slug"])
                if slug in self.outcome.products:
                    continue
                self.outcome.products[slug] = int(node["id"])
                if node.get("type") == "variable":
                    async for row in self._pages(f"products/{node['id']}/variations"):
                        if row.get("sku"):
                            self.outcome.variations[row["sku"]] = (int(node["id"]), int(row["id"]))

    async def _index_seeded_orders(self) -> None:
        """Record the seed refs already on the site. Orders have no slug to match on, so
        every one is read and its meta inspected; the site is a demo store, small enough
        that listing them costs less than the duplicates it prevents."""
        async for node in self._pages("orders", status="any"):
            for meta in node.get("meta_data") or []:
                if meta.get("key") == SEED_REF_META_KEY and meta.get("value"):
                    self.outcome.seeded_refs.add(str(meta["value"]))

    # -- Products ----------------------------------------------------------------------

    def product_payload(self, entry: dict[str, Any], category_id: int | None) -> dict[str, Any]:
        kind = entry.get("type") or "simple"
        status = entry.get("status") or "publish"
        payload: dict[str, Any] = {
            "name": entry["name"],
            "slug": entry["slug"],
            "type": kind,
            # There is no "create as trashed"; the product is trashed after creation.
            "status": "publish" if status == "trash" else status,
            "sku": entry.get("sku") or "",
            "description": entry.get("description") or "",
            "short_description": entry.get("short_description") or "",
            "categories": [{"id": category_id}] if category_id else [],
        }
        if kind == "variable":
            payload["attributes"] = [
                {
                    "name": attribute["name"],
                    "visible": True,
                    "variation": True,
                    "options": list(attribute["options"]),
                }
                for attribute in entry.get("attributes") or []
            ]
        else:
            payload["regular_price"] = str(entry.get("regular_price") or "")
            payload.update(self._stock_fields(entry))
            if meta := _cost_meta(entry.get("cost")):
                payload["meta_data"] = meta
        image_count = int(entry.get("images", 0))
        if self.images and image_count > 0:
            payload["images"] = [
                {"src": f"{IMAGE_HOST}/{entry['slug']}-{n}.jpg"} for n in range(1, image_count + 1)
            ]
        return payload

    @staticmethod
    def _stock_fields(entry: dict[str, Any]) -> dict[str, Any]:
        if bool(entry.get("manage_stock", True)):
            return {"manage_stock": True, "stock_quantity": int(entry.get("stock_quantity") or 0)}
        return {"manage_stock": False, "stock_status": entry.get("stock_status") or "instock"}

    @staticmethod
    def variation_payload(row: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sku": row.get("sku") or "",
            "regular_price": str(row.get("regular_price") or ""),
            "manage_stock": True,
            "stock_quantity": int(row.get("stock_quantity") or 0),
            "attributes": [
                {"name": name, "option": option}
                for name, option in (row.get("attributes") or {}).items()
            ],
        }
        if meta := _cost_meta(row.get("cost")):
            payload["meta_data"] = meta
        return payload

    async def _create_product(self, entry: dict[str, Any], category_id: int | None) -> None:
        payload = self.product_payload(entry, category_id)
        try:
            created = await self._post("products", payload)
        except WooApiError as error:
            if not payload.get("images"):
                raise
            # Usually a failed image sideload; the product matters more than its pictures.
            self.echo(f"retry:   {entry['slug']} without images ({error})")
            payload.pop("images")
            created = await self._post("products", payload)
        product_id = int(created["id"])
        self.outcome.products[entry["slug"]] = product_id
        self.echo(f"created: {entry['slug']} → {product_id}")

        for row in entry.get("variations") or []:
            variation = await self._post(
                f"products/{product_id}/variations", self.variation_payload(row)
            )
            self.outcome.variations[row["sku"]] = (product_id, int(variation["id"]))
            self.echo(f"         variation {row['sku']} → {variation['id']}")

        if entry.get("status") == "trash":
            await self.transport.request(
                "DELETE", f"{WC_V3}/products/{product_id}", params={"force": "false"}
            )
            self.echo(f"         trashed {entry['slug']}")

    # -- Orders ------------------------------------------------------------------------

    def _line_items(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        lines = []
        for line in entry["lines"]:
            product_id = self.outcome.products.get(line["slug"])
            if product_id is None:
                continue
            item = {"product_id": product_id, "quantity": int(line["quantity"])}
            if variation := self.outcome.variations.get(line.get("variation") or ""):
                item["variation_id"] = variation[1]
            lines.append(item)
        return lines

    @staticmethod
    def order_payload(entry: dict[str, Any], lines: list[dict[str, Any]]) -> dict[str, Any]:
        status = entry.get("status") or "completed"
        return {
            # A refunded order is created paid and then refunded in a second request.
            "status": "processing" if status == "refunded" else status,
            "set_paid": status in PAID_STATUSES,
            "line_items": lines,
            "customer_note": entry.get("customer_note") or "",
            "meta_data": [{"key": SEED_REF_META_KEY, "value": entry["ref"]}],
            "billing": {
                "first_name": "Sample",
                "last_name": "Buyer",
                "email": "buyer@example.test",
            },
        }

    async def _place_orders(self) -> None:
        kept = 0
        for entry in self.spec["orders"]:
            if entry["ref"] in self.outcome.seeded_refs:
                kept += 1
                continue
            lines = self._line_items(entry)
            if not lines:
                continue
            payload = self.order_payload(entry, lines)
            order = await self._post("orders", payload)
            order_id = int(order["id"])
            self.outcome.orders.append(order_id)
            self.outcome.seeded_refs.add(entry["ref"])
            self.echo(f"order:   #{order_id} ({entry.get('status') or 'completed'})")
            if amount := entry.get("refunded"):
                await self._post(
                    f"orders/{order_id}/refunds",
                    {"amount": str(amount), "api_refund": False, "reason": "seed"},
                )
                self.echo(f"         refunded {amount}")
        if kept:
            self.echo(f"kept:    {kept} orders already seeded on the site")
        if self.outcome.orders:
            self.echo(f"{len(self.outcome.orders)} orders placed today.")


# -- Command line ------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed a WooCommerce site from data/seed.json.")
    parser.add_argument("--dry-run", action="store_true", help="print the requests; send nothing")
    parser.add_argument("--skip-orders", action="store_true", help="create the catalog only")
    parser.add_argument("--no-images", action="store_true", help="leave product images out")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    spec = load_seed()
    options = {"images": not args.no_images, "orders": not args.skip_orders}

    if args.dry_run:
        transport = RecordingTransport()
        await Seeder(transport, spec, echo=lambda _: None, **options).run()
        transport.dump()
        return 0

    try:
        settings = load_settings()
    except MissingCredentials as error:
        print(error)
        return 2
    if settings.local_store:
        print("WOOCOMMERCE_LOCAL_STORE is set: the local store seeds itself; nothing to do.")
        return 0

    client = WooRestClient(
        settings.store_url,
        consumer_key=settings.consumer_key,
        consumer_secret=settings.consumer_secret,
    )
    try:
        await Seeder(client, spec, **options).run()
    finally:
        await client.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
