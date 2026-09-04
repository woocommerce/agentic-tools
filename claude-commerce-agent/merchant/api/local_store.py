# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""In-memory stand-in for a WooCommerce site's REST API, for running without WordPress.

Setting ``WOOCOMMERCE_LOCAL_STORE=1`` makes ``create_merchant_portal`` use this class as
the executor instead of ``WooRestClient``. Every other component is unchanged: the backend,
caches, ledger, guardrails, router, and portal all run exactly as they would against a real
site, and this object answers the ``wc/v3`` and ``wc-analytics`` requests they make from
plain Python data. Writes mutate that data, so the approval flow can be exercised end to
end: stage a price change, click Approve, and the next catalog read returns the new price.

It is not WooCommerce and does not try to be. It implements the routes this deployment
uses (products, variations, categories, reviews, orders, refunds, and the revenue stats
report) and validates far less than a real site does. To exercise the real API,
``wordpress/local-store/`` starts a disposable site in Docker and ``scripts/smoke_live.py``
runs against it. The storefront tests' Store API fake in
``storefront/api/tests/fake_store.py`` reads its products from this module, so both
services see one catalog.

Because it controls ``date_created_gmt``, this store can also have what a freshly seeded
real site lacks: back-dated orders, which give the snapshot a comparison period and the
return-spike rule something to trigger on.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .rest_client import WooApiError

# Noon UTC, so subtracting whole and fractional days keeps each generated order inside the
# intended calendar day no matter when the process started.
TODAY = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)

CURRENCY = "USD"
# Depth of the generated order history. Enough for the default snapshot's previous-period
# comparison and for the 30 sparkline points on the portal.
HISTORY_DAYS = 60
# Constant RNG seed: the same seed.json always yields the same orders, so tests can pin
# numbers.
HISTORY_SEED = 20260828
IMAGE_HOST = "https://images.acme-supply.invalid"

DEFAULT_STORE_NAME = "ACME Supply Co."
DEFAULT_STORE_URL = "http://acme-supply.local"
FIRST_PRODUCT_ID = 101
FIRST_CATEGORY_ID = 11
FIRST_ORDER_ID = 1001
# Ids handed out for anything created through the API, well clear of the seed ranges.
FIRST_CREATED_ID = 5001
# The product kept off the generated history so it stays the seed's slow mover.
_SLOW_MOVER_SLUG = "cast-iron-hold-down"
_PAID_STATUSES = frozenset({"processing", "completed", "refunded"})
TRASHED_SUFFIX = "__trashed"
_NOT_REVENUE = frozenset({"cancelled", "failed", "refunded", "pending", "trash"})
_INTERVAL_DAYS = {"day": 1, "week": 7, "month": 30}
_DAY = "%Y-%m-%d"
_STAMP = "%Y-%m-%dT%H:%M:%S"

Payload = tuple[Any, dict[str, str]]


def gmt(days_ago: float) -> str:
    """Timestamp in WooCommerce's ``*_gmt`` format: ISO 8601 with no timezone suffix."""
    return (TODAY - timedelta(days=days_ago)).strftime(_STAMP)


def _money(value: Any) -> str:
    return "" if value in (None, "") else f"{Decimal(str(value)):.2f}"


def _paragraph(text: str | None) -> str:
    return f"<p>{text}</p>" if text else ""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _whole(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _no_such_id(code: str) -> WooApiError:
    return WooApiError("Invalid ID.", code=code, status=404)


class _NoRoute(LookupError):
    """Raised inside the dispatcher for a path or method this store does not answer."""


# -- Node builders -------------------------------------------------------------------------
# These produce the JSON shapes ``wc/v3`` returns. The storefront's Store API fake reads
# the same nodes, so field names here are the contract between the two test suites.


def category_node(term_id: int, slug: str, name: str) -> dict[str, Any]:
    return {"id": term_id, "name": name, "slug": slug, "parent": 0, "count": 0}


def _sale_fields() -> dict[str, Any]:
    return {"sale_price": "", "on_sale": False, "date_on_sale_from": None, "date_on_sale_to": None}


def _cost_meta(cost: Any) -> list[dict[str, Any]]:
    if cost in (None, ""):
        return []
    return [{"id": 0, "key": "_wc_cog_cost", "value": _money(cost)}]


def _stock_fields(entry: dict[str, Any]) -> dict[str, Any]:
    managed = bool(entry.get("manage_stock", True))
    quantity = int(entry.get("stock_quantity") or 0) if managed else None
    status = entry.get("stock_status")
    if not status:
        status = "outofstock" if managed and (quantity or 0) <= 0 else "instock"
    return {"manage_stock": managed, "stock_quantity": quantity, "stock_status": status}


def _unmanaged_stock() -> dict[str, Any]:
    return {"manage_stock": False, "stock_quantity": None, "stock_status": "instock"}


def variation_node(
    variation_id: int, parent_id: int, entry: dict[str, Any], sku_prefix: str = ""
) -> dict[str, Any]:
    regular = _money(entry.get("regular_price"))
    return {
        "id": variation_id,
        "parent_id": parent_id,
        "type": "variation",
        "status": entry.get("status") or "publish",
        "sku": entry.get("sku") or f"{sku_prefix}-{variation_id}",
        "price": regular,
        "regular_price": regular,
        **_sale_fields(),
        **_stock_fields(entry),
        "attributes": [
            {"id": 0, "name": name, "option": option}
            for name, option in (entry.get("attributes") or {}).items()
        ],
        "image": None,
        "date_modified_gmt": gmt(9),
        "meta_data": _cost_meta(entry.get("cost")),
    }


def product_node(
    product_id: int, entry: dict[str, Any], categories: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Build a ``wc/v3`` product node from a seed entry. Variation ids for a variable
    product are derived from the parent id and listed under ``variations``."""
    kind = entry.get("type") or "simple"
    variable = kind == "variable"
    term = categories.get(entry.get("category") or "")
    regular = "" if variable else _money(entry.get("regular_price"))
    return {
        "id": product_id,
        "name": entry["name"],
        "slug": entry["slug"],
        "type": kind,
        "status": entry.get("status") or "publish",
        "sku": entry.get("sku") or "",
        "price": regular,
        "regular_price": regular,
        **_sale_fields(),
        "description": _paragraph(entry.get("description")),
        "short_description": _paragraph(entry.get("short_description")),
        "images": [
            {
                "id": product_id * 10 + index,
                "src": f"{IMAGE_HOST}/{entry['slug']}-{index + 1}.jpg",
                "alt": "",
            }
            for index in range(int(entry.get("images", 1)))
        ],
        "categories": [dict(term)] if term else [],
        "attributes": [
            {
                "id": 0,
                "name": attribute["name"],
                "position": position,
                "visible": True,
                "variation": True,
                "options": list(attribute["options"]),
            }
            for position, attribute in enumerate(entry.get("attributes") or [])
        ],
        "variations": [],
        "date_modified_gmt": gmt(9),
        "meta_data": [] if variable else _cost_meta(entry.get("cost")),
        "average_rating": "0.00",
        "rating_count": 0,
        **(_unmanaged_stock() if variable else _stock_fields(entry)),
    }


# One order line as the builders pass it around: product node, variation node or None,
# quantity.
OrderLineSpec = tuple[dict[str, Any], dict[str, Any] | None, int]


def _line_item(order_id: int, position: int, spec: OrderLineSpec) -> tuple[dict[str, Any], Decimal]:
    product, variation, quantity = spec
    unit_price = Decimal((variation or product)["price"] or "0")
    line_total = unit_price * quantity
    name = product["name"]
    if variation:
        name += " - " + ", ".join(entry["option"] for entry in variation["attributes"])
    row = {
        "id": order_id * 10 + position,
        "name": name,
        "product_id": product["id"],
        "variation_id": variation["id"] if variation else 0,
        "quantity": quantity,
        "total": f"{line_total:.2f}",
    }
    return row, line_total


def order_node(
    number: int,
    *,
    days_ago: float,
    status: str,
    lines: list[OrderLineSpec],
    refunded: str | None = None,
    customer_note: str | None = None,
    meta_data: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a ``wc/v3`` order node. Line totals are unit price times quantity, taken from
    the product or variation node, so revenue figures reconcile with the catalog."""
    rows: list[dict[str, Any]] = []
    total = Decimal("0")
    for position, spec in enumerate(lines):
        row, line_total = _line_item(number, position, spec)
        rows.append(row)
        total += line_total
    refunds = []
    if refunded:
        refunds.append({"id": number * 100, "reason": "", "total": f"-{Decimal(refunded):.2f}"})
    return {
        "id": number,
        "number": str(number),
        "status": status,
        "currency": CURRENCY,
        "date_created_gmt": gmt(days_ago),
        "date_paid_gmt": gmt(days_ago) if status in _PAID_STATUSES else None,
        "total": f"{total:.2f}",
        "customer_note": customer_note or "",
        "meta_data": list(meta_data or []),
        "line_items": rows,
        "refunds": refunds,
    }


REVIEWS = [
    {
        "id": 1,
        "product_id": 101,
        "status": "approved",
        "reviewer": "A buyer",
        "rating": 5,
        "review": "<p>Pockets in the right places and the straps do not dig in. Wore it all week.</p>",
    },
    {
        "id": 2,
        "product_id": 101,
        "status": "approved",
        "reviewer": "A buyer",
        "rating": 4,
        "review": "<p>Heavier than I expected, which is good for a shop apron. Wish it came in a shorter cut.</p>",
    },
]


# -- Seed data -----------------------------------------------------------------------------


class SeedCatalog:
    """``seed.json`` turned into ``wc/v3`` nodes with deterministic ids: categories from 11,
    products from 101 in file order, and a product's variations numbered ``id * 10 + n``,
    which keeps the two ranges disjoint."""

    def __init__(self, seed: dict[str, Any]) -> None:
        self._seed = seed
        self.categories: dict[str, dict[str, Any]] = {
            entry["slug"]: category_node(FIRST_CATEGORY_ID + offset, entry["slug"], entry["name"])
            for offset, entry in enumerate(seed.get("categories") or [])
        }
        self.products: list[dict[str, Any]] = []
        self.variations: dict[int, list[dict[str, Any]]] = {}
        for product_id, entry in enumerate(seed["products"], start=FIRST_PRODUCT_ID):
            self.products.append(self._product(product_id, entry))

    @property
    def store_name(self) -> str:
        return (self._seed.get("store") or {}).get("name") or DEFAULT_STORE_NAME

    def _product(self, product_id: int, entry: dict[str, Any]) -> dict[str, Any]:
        node = product_node(product_id, entry, self.categories)
        if node["type"] == "variable":
            rows = [
                variation_node(product_id * 10 + position, product_id, spec, entry.get("sku") or "")
                for position, spec in enumerate(entry.get("variations") or [], start=1)
            ]
            self.variations[product_id] = rows
            node["variations"] = [row["id"] for row in rows]
        return node

    def orders(self) -> list[dict[str, Any]]:
        """Seed orders first, then generated history behind them.

        The orders listed in the seed file exercise the alert rules deliberately: enough
        refunds to trip the return-spike threshold, one order stuck in processing, one with
        a customer note. Behind those, ``HISTORY_DAYS`` of orders are generated from a fixed
        RNG seed over the sellable products, giving the snapshot a previous period and the
        sparklines a shape. None of it is real sales data."""
        listed = self._seed["orders"]
        orders = self._listed_orders(listed)
        oldest = max((float(entry.get("days_ago", 1)) for entry in listed), default=1.0)
        history = _HistoryGenerator(self._sellable(), FIRST_ORDER_ID + len(orders))
        return orders + history.generate(first_day=int(oldest) + 1, last_day=HISTORY_DAYS)

    def _variation_by_sku(self, sku: str | None) -> dict[str, Any] | None:
        if not sku:
            return None
        for rows in self.variations.values():
            for row in rows:
                if row["sku"] == sku:
                    return row
        return None

    def _listed_orders(self, listed: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_slug = {product["slug"]: product for product in self.products}
        orders: list[dict[str, Any]] = []
        for entry in listed:
            lines: list[OrderLineSpec] = []
            for line in entry["lines"]:
                product = by_slug.get(line["slug"])
                if product is not None:
                    variation = self._variation_by_sku(line.get("variation"))
                    lines.append((product, variation, int(line["quantity"])))
            if not lines:
                continue
            orders.append(
                order_node(
                    FIRST_ORDER_ID + len(orders),
                    days_ago=float(entry.get("days_ago", 1)),
                    status=entry.get("status") or "completed",
                    lines=lines,
                    refunded=entry.get("refunded"),
                    customer_note=entry.get("customer_note"),
                )
            )
        return orders

    def _sellable(self) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
        """What generated orders may contain: published simple products with a price, then
        every variation of a published variable product."""
        published = [product for product in self.products if product["status"] == "publish"]
        simple = [
            (product, None)
            for product in published
            if product["type"] != "variable" and Decimal(product["price"] or "0") > 0
        ]
        variations = [
            (product, row)
            for product in published
            for row in self.variations.get(product["id"], [])
        ]
        return simple + variations


class _HistoryGenerator:
    """Back-dated ``completed`` orders from a seeded RNG. Fewer orders land on Saturday and
    Sunday, so week-over-week numbers move and the daily sparkline shows a pattern instead
    of uniform noise."""

    def __init__(self, sellable: list[tuple[dict[str, Any], dict[str, Any] | None]], next_id: int):
        self._sellable = sellable
        self._next_id = next_id
        self._rng = random.Random(HISTORY_SEED)

    def generate(self, *, first_day: int, last_day: int) -> list[dict[str, Any]]:
        orders: list[dict[str, Any]] = []
        for days_back in range(first_day, last_day):
            weekend = (TODAY - timedelta(days=days_back)).weekday() >= 5
            count = self._rng.choice((0, 1, 1, 2) if weekend else (1, 2, 2, 3))
            for _ in range(count):
                orders.append(self._order(days_back))
        return orders

    def _order(self, days_back: int) -> dict[str, Any]:
        order_id = self._next_id
        self._next_id += 1
        how_many = min(len(self._sellable), self._rng.choice((1, 1, 2)))
        picked = self._rng.sample(self._sellable, k=how_many)
        picked = [pair for pair in picked if pair[0]["slug"] != _SLOW_MOVER_SLUG] or [
            self._sellable[0]
        ]
        placed = days_back + self._rng.random() * 0.4
        lines: list[OrderLineSpec] = [
            (product, variation, self._rng.choice((1, 1, 1, 2))) for product, variation in picked
        ]
        return order_node(order_id, days_ago=placed, status="completed", lines=lines)


# -- Paging --------------------------------------------------------------------------------


def _page(items: list[dict[str, Any]], params: dict[str, Any]) -> Payload:
    per_page = min(max(_whole(params.get("per_page"), 10), 1), 100)
    page = max(_whole(params.get("page"), 1), 1)
    pages = max(1, -(-len(items) // per_page))
    offset = (page - 1) * per_page
    headers = {"X-WP-Total": str(len(items)), "X-WP-TotalPages": str(pages)}
    return items[offset : offset + per_page], headers


def _single(payload: Any) -> Payload:
    return payload, {}


# -- Field writes --------------------------------------------------------------------------

_PRODUCT_FIELDS = frozenset(
    {
        "name",
        "slug",
        "status",
        "sku",
        "regular_price",
        "sale_price",
        "date_on_sale_from",
        "date_on_sale_to",
        "manage_stock",
        "stock_quantity",
        "stock_status",
        "description",
        "short_description",
        "categories",
        "attributes",
        "meta_data",
        "type",
        "images",
    }
)
_VARIATION_FIELDS = frozenset(
    {
        "status",
        "sku",
        "regular_price",
        "sale_price",
        "date_on_sale_from",
        "date_on_sale_to",
        "manage_stock",
        "stock_quantity",
        "stock_status",
        "attributes",
        "meta_data",
        "image",
    }
)


def _reprice(node: dict[str, Any]) -> None:
    """Recompute ``price`` and ``on_sale`` the way WooCommerce does: the sale price applies
    when one is set and today falls inside its date window, if it has one."""
    sale = node.get("sale_price") or ""
    today = TODAY.strftime(_DAY)
    starts = (node.get("date_on_sale_from") or "")[:10]
    ends = (node.get("date_on_sale_to") or "")[:10]
    live = bool(sale) and (not starts or starts <= today) and (not ends or ends >= today)
    node["on_sale"] = live
    node["price"] = sale if live else (node.get("regular_price") or "")


def _settle_stock_status(node: dict[str, Any]) -> None:
    if node.get("manage_stock") and node.get("stock_quantity") is not None:
        node["stock_status"] = "instock" if node["stock_quantity"] > 0 else "outofstock"


# -- The store -----------------------------------------------------------------------------


@dataclass
class LocalStore:
    """An in-memory WooCommerce store answering the REST calls this deployment makes.

    Reads come from ``products``, ``variations``, and ``orders``; writes update those lists
    in place. Nothing is persisted, so restarting the process resets the store. That is
    what a demo wants, and a reminder that this is not a database."""

    categories: list[dict[str, Any]]
    products: list[dict[str, Any]]
    variations: dict[int, list[dict[str, Any]]]
    orders: list[dict[str, Any]]
    store_name: str = DEFAULT_STORE_NAME
    store_url: str = DEFAULT_STORE_URL
    reviews: list[dict[str, Any]] = field(default_factory=lambda: [dict(r) for r in REVIEWS])
    # Log of every write as ``(method, path, body)``, for the smoke script and the tests.
    applied: list[tuple[str, str, Any]] = field(default_factory=list)
    _next_id: int = FIRST_CREATED_ID - 1

    @classmethod
    def from_seed(cls, seed_path: Path, *, store_url: str = DEFAULT_STORE_URL) -> LocalStore:
        catalog = SeedCatalog(json.loads(seed_path.read_text(encoding="utf-8")))
        return cls(
            categories=list(catalog.categories.values()),
            products=catalog.products,
            variations=catalog.variations,
            orders=catalog.orders(),
            store_name=catalog.store_name,
            store_url=store_url,
        )

    @classmethod
    def empty(cls, *, store_url: str = DEFAULT_STORE_URL) -> LocalStore:
        """An empty store, for the seeding script to populate."""
        return cls(categories=[], products=[], variations={}, orders=[], store_url=store_url)

    # -- Transport ---------------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Payload:
        route = "/".join(part for part in path.strip("/").split("/") if part)
        verb = method.upper()
        for pattern, handler in self._routes():
            match = pattern.fullmatch(route)
            if match is None:
                continue
            try:
                return handler(verb, params or {}, json, *map(int, match.groups()))
            except _NoRoute:
                break
        raise WooApiError(
            f"no answer in the local store for {verb} /{path}", code="rest_no_route", status=404
        )

    def _routes(self) -> list[tuple[re.Pattern[str], Callable[..., Payload]]]:
        return [
            (re.compile(r""), self._site_index),
            (re.compile(r"wc/v3/system_status"), self._system_status),
            (re.compile(r"wc/v3/products/categories"), self._categories),
            (re.compile(r"wc/v3/products/reviews"), self._reviews),
            (re.compile(r"wc/v3/products"), self._product_collection),
            (re.compile(r"wc/v3/products/(\d+)"), self._product_item),
            (re.compile(r"wc/v3/products/(\d+)/variations"), self._variation_collection),
            (re.compile(r"wc/v3/products/(\d+)/variations/(\d+)"), self._variation_item),
            (re.compile(r"wc/v3/orders"), self._order_collection),
            (re.compile(r"wc/v3/orders/(\d+)"), self._order_item),
            (re.compile(r"wc/v3/orders/(\d+)/refunds"), self._refunds),
            (re.compile(r"wc-analytics/reports/revenue/stats"), self._revenue_stats),
        ]

    def _record(self, method: str, path: str, body: Any) -> None:
        self.applied.append((method, path, body))

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    # -- Site ----------------------------------------------------------------------------

    def _site_index(self, method: str, params: dict, body: Any) -> Payload:
        if method != "GET":
            raise _NoRoute
        return _single(
            {
                "name": self.store_name,
                "description": "A workshop supply store",
                "url": self.store_url,
                "home": self.store_url,
                "timezone_string": "America/Toronto",
                "namespaces": [
                    "wp/v2",
                    "wc/v3",
                    "wc/store/v1",
                    "wc-analytics",
                    "claude-commerce/v1",
                ],
                "site_logo": 0,
                "site_icon_url": "",
            }
        )

    def _system_status(self, method: str, params: dict, body: Any) -> Payload:
        if method != "GET":
            raise _NoRoute
        return _single(
            {
                "environment": {
                    "home_url": self.store_url,
                    "site_url": self.store_url,
                    "version": "10.4.0",
                    "wp_version": "6.9",
                },
                "settings": {"currency": CURRENCY, "currency_symbol": "$"},
            }
        )

    # -- Categories and reviews ------------------------------------------------------------

    def _categories(self, method: str, params: dict, body: Any) -> Payload:
        if method == "POST":
            term = category_node(
                self._new_id(), body.get("slug") or _slug(body["name"]), body["name"]
            )
            self.categories.append(term)
            self._record("POST", "products/categories", body)
            return _single(term)
        if method != "GET":
            raise _NoRoute
        terms = self.categories
        if needle := str(params.get("search") or "").casefold():
            terms = [t for t in terms if needle in t["name"].casefold() or needle in t["slug"]]
        if slug := params.get("slug"):
            terms = [t for t in terms if t["slug"] == slug]
        return _page(terms, params)

    def _reviews(self, method: str, params: dict, body: Any) -> Payload:
        if method != "GET":
            raise _NoRoute
        product = str(params.get("product") or "")
        rows = [r for r in self.reviews if not product or str(r["product_id"]) == product]
        return _page(rows, params)

    # -- Products ----------------------------------------------------------------------------

    def _product(self, product_id: int) -> dict[str, Any]:
        for node in self.products:
            if node["id"] == product_id:
                return node
        raise _no_such_id("woocommerce_rest_product_invalid_id")

    def _post(self, post_id: int) -> dict[str, Any]:
        """A product or a variation by post id; both live in one WordPress id space."""
        for node in self.products:
            if node["id"] == post_id:
                return node
        for rows in self.variations.values():
            for row in rows:
                if row["id"] == post_id:
                    return row
        raise _no_such_id("woocommerce_rest_product_invalid_id")

    def _product_collection(self, method: str, params: dict, body: Any) -> Payload:
        if method == "POST":
            return _single(self._create_product(body))
        if method != "GET":
            raise _NoRoute
        wanted_status = str(params.get("status") or "any")
        rows = [
            node
            for node in self.products
            if (
                node["status"] != "trash"
                if wanted_status == "any"
                else node["status"] == wanted_status
            )
        ]
        if (slug := params.get("slug")) is not None:
            rows = [node for node in rows if node["slug"] == slug]
        if needle := str(params.get("search") or "").casefold():
            terms = [term for term in re.split(r"\s+", needle) if term]
            rows = [node for node in rows if _mentions(node, terms)]
        if params.get("orderby") == "title":
            rows = sorted(rows, key=lambda node: node["name"].casefold())
        return _page(rows, params)

    def _product_item(self, method: str, params: dict, body: Any, product_id: int) -> Payload:
        if method == "GET":
            return _single(self._post(product_id))
        if method == "PUT":
            node = self._product(product_id)
            self._write_fields(node, body, variation=False)
            self._record("PUT", f"products/{product_id}", body)
            return _single(node)
        if method == "DELETE":
            node = self._product(product_id)
            if str(params.get("force") or "").lower() in {"1", "true"}:
                self.products.remove(node)
            else:
                node["status"] = "trash"
                # WordPress frees the slug for reuse by renaming the trashed post's.
                if not node["slug"].endswith(TRASHED_SUFFIX):
                    node["slug"] += TRASHED_SUFFIX
            self._record("DELETE", f"products/{product_id}", params)
            return _single(node)
        raise _NoRoute

    def _variation_collection(
        self, method: str, params: dict, body: Any, product_id: int
    ) -> Payload:
        parent = self._product(product_id)
        rows = self.variations.setdefault(parent["id"], [])
        if method == "POST":
            return _single(self._create_variation(parent, body))
        if method != "GET":
            raise _NoRoute
        return _page([row for row in rows if row["status"] != "trash"], params)

    def _variation_item(
        self, method: str, params: dict, body: Any, product_id: int, variation_id: int
    ) -> Payload:
        parent = self._product(product_id)
        rows = self.variations.setdefault(parent["id"], [])
        row = next((r for r in rows if r["id"] == variation_id), None)
        if row is None:
            raise _no_such_id("woocommerce_rest_invalid_id")
        if method == "GET":
            return _single(row)
        if method == "PUT":
            self._write_fields(row, body, variation=True)
            self._record("PUT", f"products/{product_id}/variations/{row['id']}", body)
            return _single(row)
        raise _NoRoute

    def _write_fields(self, node: dict[str, Any], body: dict[str, Any], *, variation: bool) -> None:
        """Apply a PUT body to a node, refusing fields WooCommerce would not accept on it,
        then settle the derived fields (stock status, price, modification time)."""
        writable = _VARIATION_FIELDS if variation else _PRODUCT_FIELDS
        for key, value in (body or {}).items():
            if key == "id":
                continue
            if key not in writable:
                raise WooApiError(
                    f"{key} is not a writable field on this store",
                    code="rest_invalid_param",
                    status=400,
                )
            node[key] = self._coerce(key, value, variation)
        _settle_stock_status(node)
        _reprice(node)
        node["date_modified_gmt"] = gmt(0)

    def _coerce(self, key: str, value: Any, variation: bool) -> Any:
        if key == "categories":
            wanted = {int(term["id"]) for term in value}
            return [dict(term) for term in self.categories if term["id"] in wanted]
        if key in {"regular_price", "sale_price"}:
            return _money(value)
        if key == "stock_quantity":
            return None if value is None else int(value)
        if key == "attributes" and variation:
            return [{"id": 0, "name": a["name"], "option": a.get("option")} for a in value]
        return value

    def _create_product(self, body: dict[str, Any]) -> dict[str, Any]:
        identity = {"name", "slug", "type", "images"}
        name = body.get("name") or "Untitled"
        entry = {
            "name": name,
            "slug": body.get("slug") or _slug(name),
            "type": body.get("type") or "simple",
            "images": 0,
        }
        node = product_node(self._new_id(), entry, {})
        node["images"] = [
            {"id": 0, "src": image.get("src", ""), "alt": ""} for image in body.get("images") or []
        ]
        rest = {key: value for key, value in body.items() if key not in identity}
        self._write_fields(node, rest, variation=False)
        self.products.append(node)
        self._record("POST", "products", body)
        return node

    def _create_variation(self, parent: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        rows = self.variations.setdefault(parent["id"], [])
        variation_id = parent["id"] * 10 + len(rows) + 1
        seed = {"regular_price": body.get("regular_price", "0")}
        node = variation_node(variation_id, parent["id"], seed, parent.get("sku") or "")
        node["attributes"] = []
        self._write_fields(node, body, variation=True)
        rows.append(node)
        parent["variations"] = [row["id"] for row in rows]
        self._record("POST", f"products/{parent['id']}/variations", body)
        return node

    # -- Orders -------------------------------------------------------------------------------

    def _order(self, order_id: int) -> dict[str, Any]:
        for node in self.orders:
            if node["id"] == order_id:
                return node
        raise _no_such_id("woocommerce_rest_shop_order_invalid_id")

    def _order_collection(self, method: str, params: dict, body: Any) -> Payload:
        if method == "POST":
            return _single(self._create_order(body))
        if method != "GET":
            raise _NoRoute
        rows = list(self.orders)
        if after := params.get("after"):
            rows = [o for o in rows if o["date_created_gmt"] >= str(after)[:19]]
        wanted_status = str(params.get("status") or "any")
        if wanted_status != "any":
            rows = [o for o in rows if o["status"] == wanted_status]
        rows.sort(key=lambda o: o["date_created_gmt"], reverse=True)
        return _page(rows, params)

    def _order_item(self, method: str, params: dict, body: Any, order_id: int) -> Payload:
        order = self._order(order_id)
        if method == "GET":
            return _single(order)
        if method == "PUT":
            for key in ("status", "customer_note"):
                if key in body:
                    order[key] = body[key]
            self._record("PUT", f"orders/{order['id']}", body)
            return _single(order)
        raise _NoRoute

    def _refunds(self, method: str, params: dict, body: Any, order_id: int) -> Payload:
        order = self._order(order_id)
        if method != "POST":
            raise _NoRoute
        amount = Decimal(str(body.get("amount") or order["total"]))
        refund = {
            "id": self._new_id(),
            "reason": body.get("reason") or "",
            "total": f"-{amount:.2f}",
        }
        order["refunds"].append(refund)
        if amount >= Decimal(order["total"]):
            order["status"] = "refunded"
        self._record("POST", f"orders/{order['id']}/refunds", body)
        return _single(refund)

    def _create_order(self, body: dict[str, Any]) -> dict[str, Any]:
        lines: list[OrderLineSpec] = []
        for line in body.get("line_items") or []:
            product = self._product(int(line["product_id"]))
            variation = None
            if line.get("variation_id"):
                wanted = int(line["variation_id"])
                rows = self.variations.get(product["id"], [])
                variation = next((row for row in rows if row["id"] == wanted), None)
            lines.append((product, variation, int(line.get("quantity") or 1)))
        status = body.get("status") or ("processing" if body.get("set_paid") else "pending")
        node = order_node(
            self._new_id(),
            days_ago=0,
            status=status,
            lines=lines,
            customer_note=body.get("customer_note"),
            meta_data=body.get("meta_data"),
        )
        self.orders.insert(0, node)
        self._record("POST", "orders", body)
        return node

    # -- Analytics ----------------------------------------------------------------------------

    def _revenue_stats(self, method: str, params: dict, body: Any) -> Payload:
        """Local version of ``wc-analytics/reports/revenue/stats``: ``totals`` over the
        requested window and, when ``interval`` is set, one ``intervals`` row per bucket,
        all computed from this store's orders."""
        if method != "GET":
            raise _NoRoute
        after = str(params.get("after") or "1970-01-01")[:10]
        before = str(params.get("before") or TODAY.strftime(_DAY))[:10]
        selected = [
            o
            for o in self.orders
            if after <= o["date_created_gmt"][:10] <= before and o["status"] not in _NOT_REVENUE
        ]
        report: dict[str, Any] = {"totals": _report_totals(selected), "intervals": []}
        step = _INTERVAL_DAYS.get(str(params.get("interval")))
        if step is not None:
            for start, stop in _buckets(
                date.fromisoformat(after), date.fromisoformat(before), step
            ):
                inside = [
                    o
                    for o in selected
                    if start.isoformat() <= o["date_created_gmt"][:10] <= stop.isoformat()
                ]
                report["intervals"].append(
                    {
                        "interval": start.isoformat(),
                        "date_start": f"{start.isoformat()} 00:00:00",
                        "date_end": f"{stop.isoformat()} 23:59:59",
                        "subtotals": _report_totals(inside),
                    }
                )
        return _single(report)


def _mentions(node: dict[str, Any], terms: list[str]) -> bool:
    """WooCommerce's ``search`` looks at title, content, and SKU; so does this."""
    haystack = (
        node["name"].casefold(),
        node["description"].casefold(),
        (node["sku"] or "").casefold(),
    )
    return any(term in text for term in terms for text in haystack)


def _buckets(start: date, end: date, step: int) -> list[tuple[date, date]]:
    spans: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=step - 1), end)
        spans.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return spans


def _report_totals(orders: list[dict[str, Any]]) -> dict[str, Any]:
    sales = sum((Decimal(o["total"]) for o in orders), Decimal("0"))
    refunds = sum(
        (abs(Decimal(r["total"])) for o in orders for r in o.get("refunds") or []), Decimal("0")
    )
    count = len(orders)
    return {
        "orders_count": count,
        "num_items_sold": sum(line["quantity"] for o in orders for line in o["line_items"]),
        "gross_sales": float(sales),
        "total_sales": float(sales),
        "net_revenue": float(sales - refunds),
        "refunds": float(refunds),
        "avg_order_value": float(sales / count) if count else 0.0,
    }
