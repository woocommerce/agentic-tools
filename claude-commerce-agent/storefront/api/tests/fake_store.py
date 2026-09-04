# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""A WooCommerce site's public API, in process, for the storefront tests.

:class:`FakeWooSite` is an ``httpx.MockTransport`` handler whose catalog is the merchant
side's :class:`LocalStore`, so both suites read the one seed file. It answers the routes the
backend uses: the root index, Store API products and categories, the cart with real
``Cart-Token`` semantics (a token per cart, writes refused without one, unknown tokens
refused with WooCommerce's own error slug), ``wp/v2`` pages, and the bridge plugin's brand
and orders routes. The request log lets a test check what was sent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import httpx
from merchant.api.agent_config import DATA_DIR
from merchant.api.local_store import LocalStore

STORE_URL = "https://acme-supply.example"
MINOR_UNIT = 2
TAGLINE = "Tools and storage for the workshop bench"
BRAND_COLOURS = {"background": "#1f2a44", "foreground": "#ffffff"}

PAGES = [
    (
        31,
        "returns-and-refunds",
        "Returns and refunds",
        "Return anything unused within 30 days of delivery for a full refund. Refunds are issued within 5 business days.",
    ),
    (
        32,
        "shipping-policy",
        "Shipping",
        "Orders ship within 2 business days. Standard shipping is $6 flat; orders over $75 ship free.",
    ),
    (33, "about", "About the workshop", "A small bench-tool maker."),
]

# WordPress drops these before matching a search phrase.
WP_STOPWORDS = frozenset(
    {
        "a", "about", "an", "are", "as", "at", "be", "by", "com", "for", "from", "how", "in",
        "is", "it", "of", "on", "or", "that", "the", "this", "to", "was", "what", "when",
        "where", "who", "will", "with", "www",
    }
)  # fmt: skip

SHIPPING_PACKAGE = {
    "package_id": 0,
    "name": "Shipping",
    "shipping_rates": [
        {
            "rate_id": "flat_rate:1",
            "name": "Standard shipping (3-5 days)",
            "price": "600",
            "method_id": "flat_rate",
            "delivery_time": "3-5 business days",
            "meta_data": [],
        },
        {
            "rate_id": "local_pickup:2",
            "name": "Pick up at the workshop",
            "price": "0",
            "method_id": "local_pickup",
            "delivery_time": "",
            "meta_data": [{"key": "pickup_address", "value": "12 Bench Lane, Toronto"}],
        },
    ],
}


class RestError(Exception):
    """What WordPress would answer: an error slug, a message, and an HTTP status."""

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code, self.status = code, status

    def response(self) -> httpx.Response:
        body = {"code": self.code, "message": str(self), "data": {"status": self.status}}
        return httpx.Response(self.status, json=body)


def minor(value: Any) -> str:
    return str(round(float(value or 0) * 10**MINOR_UNIT))


def slug_of(text: str) -> str:
    return text.lower().replace(" ", "-")


def in_stock(node: dict[str, Any]) -> bool:
    if node.get("manage_stock") and node.get("stock_quantity") is not None:
        return node["stock_quantity"] > 0
    return node.get("stock_status", "instock") == "instock"


def price_block(node: dict[str, Any]) -> dict[str, Any]:
    regular = node["regular_price"]
    return {
        "price": minor(node["price"] or regular),
        "regular_price": minor(regular),
        "sale_price": minor(node["sale_price"] or regular),
        "price_range": None,
        "currency_code": "USD",
        "currency_symbol": "$",
        "currency_minor_unit": MINOR_UNIT,
    }


def option_suffix(row: dict[str, Any]) -> str:
    return ", ".join(a["option"] for a in row["attributes"])


class CartBook:
    """Every open cart, by token. Lines are the fake's own shape: key, id, parent, name,
    price, quantity, variation pairs, image."""

    def __init__(self) -> None:
        self._carts: dict[str, list[dict[str, Any]]] = {}
        self._issued = 0
        self._keys = 0

    def __contains__(self, token: object) -> bool:
        return token in self._carts

    def __getitem__(self, token: str) -> list[dict[str, Any]]:
        return self._carts[token]

    def open(self) -> str:
        self._issued += 1
        token = f"tok-{self._issued}"
        self._carts[token] = []
        return token

    def drop(self, token: str) -> None:
        self._carts.pop(token, None)

    def lines_of(self, token: str) -> list[dict[str, Any]]:
        return self._carts.get(token, [])

    def require(self, token: str | None) -> list[dict[str, Any]]:
        if token is None:
            raise RestError(
                "woocommerce_rest_missing_nonce",
                "Missing the Nonce header. This endpoint requires a valid nonce.",
                401,
            )
        if token not in self._carts:
            raise RestError("woocommerce_rest_invalid_cart_token", "Invalid cart token.", 403)
        return self._carts[token]

    def add(self, token: str, node: dict[str, Any], parent: dict[str, Any], quantity: int) -> None:
        lines = self.require(token)
        for line in lines:
            if line["id"] == node["id"]:
                line["quantity"] += quantity
                return
        self._keys += 1
        is_variation = node is not parent
        lines.append(
            {
                "key": f"key-{self._keys}",
                "id": node["id"],
                "parent": parent["id"] if is_variation else None,
                "name": parent["name"] + (f" - {option_suffix(node)}" if is_variation else ""),
                "price": node["price"] or node["regular_price"],
                "quantity": quantity,
                "variation": [
                    {"attribute": a["name"], "value": a["option"]}
                    for a in node.get("attributes", [])
                ]
                if is_variation
                else [],
                "image": parent["images"][0]["src"] if parent["images"] else None,
            }
        )

    def set_quantity(self, token: str, key: str | None, quantity: int) -> None:
        for line in self.require(token):
            if line["key"] == key:
                line["quantity"] = quantity
                return
        raise RestError("woocommerce_rest_cart_invalid_key", "Cart item does not exist.", 409)

    def remove(self, token: str, key: str | None) -> None:
        lines = self.require(token)
        lines[:] = [line for line in lines if line["key"] != key]


Handler = Callable[[re.Match[str], dict[str, Any], dict[str, Any], str | None], httpx.Response]


class FakeWooSite:
    """The site. ``bridge`` decides whether the root index advertises the plugin's
    namespace; ``log`` records ``(method, path, params-or-body)`` per request."""

    def __init__(self, *, bridge: bool = True, store: LocalStore | None = None) -> None:
        self.store = store or LocalStore.from_seed(DATA_DIR / "seed.json", store_url=STORE_URL)
        self.bridge = bridge
        self.carts = CartBook()
        self.orders: dict[str, list[dict[str, Any]]] = {}
        self.log: list[tuple[str, str, dict[str, Any]]] = []
        self._routes: list[tuple[str, re.Pattern[str], Handler]] = [
            ("GET", re.compile(r"^$"), self._root),
            ("GET", re.compile(r"^claude-commerce/v1/brand$"), self._brand),
            ("GET", re.compile(r"^claude-commerce/v1/orders$"), self._orders),
            ("GET", re.compile(r"^wc/store/v1/products/categories$"), self._categories),
            ("GET", re.compile(r"^wc/store/v1/products$"), self._products),
            ("GET", re.compile(r"^wc/store/v1/products/(\d+)$"), self._product),
            ("GET", re.compile(r"^wc/store/v1/cart$"), self._cart),
            ("POST", re.compile(r"^wc/store/v1/cart/([a-z-]+)$"), self._cart_write),
            ("GET", re.compile(r"^wp/v2/pages$"), self._pages),
        ]

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    # -- Test controls ------------------------------------------------------------------

    def expire_cart(self, token: str) -> None:
        """WooCommerce forgot the cart: its token is no longer valid."""
        self.carts.drop(token)

    def sent(self, path: str) -> list[dict[str, Any]]:
        """The params (or JSON body) of every request to ``path``, in order."""
        return [params for _, seen, params in self.log if seen == path]

    def place_order(self, token: str, *, status: str = "processing") -> dict[str, Any]:
        """The shopper checked out from this cart; the bridge will list the order under the
        same token."""
        lines = self.carts.lines_of(token)
        number = 900 + sum(len(v) for v in self.orders.values()) + 1
        line_total = lambda line: float(line["price"]) * line["quantity"]  # noqa: E731
        order = {
            "id": number,
            "number": str(number),
            "status": status,
            "currency": "USD",
            "total": f"{sum(map(line_total, lines)):.2f}",
            "date_created_gmt": "2026-09-01T10:00:00",
            "line_items": [
                {
                    "product_id": int(line["parent"] or line["id"]),
                    "variation_id": int(line["id"]) if line["parent"] else 0,
                    "name": line["name"],
                    "quantity": line["quantity"],
                    "total": f"{line_total(line):.2f}",
                }
                for line in lines
            ],
            "tracking_url": None,
        }
        self.orders.setdefault(token, []).append(order)
        return order

    # -- Dispatch -----------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/wp-json").strip("/")
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        body = json.loads(request.content) if request.content else {}
        self.log.append((request.method, path, params or body))
        token = request.headers.get("Cart-Token")
        for method, pattern, handler in self._routes:
            match = pattern.match(path)
            if method == request.method and match:
                try:
                    return handler(match, params, body, token)
                except RestError as refused:
                    return refused.response()
        return RestError(
            "rest_no_route", f"No route was found matching the URL {path}", 404
        ).response()

    @staticmethod
    def _ok(payload: Any, **headers: str) -> httpx.Response:
        return httpx.Response(200, json=payload, headers=headers)

    # -- Root index and bridge ------------------------------------------------------------

    def _root(self, match, params, body, token) -> httpx.Response:
        namespaces = ["wp/v2", "wc/store/v1", "wc/v3"] + (
            ["claude-commerce/v1"] if self.bridge else []
        )
        return self._ok(
            {
                "name": self.store.store_name,
                "description": TAGLINE,
                "url": STORE_URL,
                "home": STORE_URL,
                "namespaces": namespaces,
                "site_logo": 0,
                "site_icon_url": f"{STORE_URL}/icon.png",
            }
        )

    def _brand(self, match, params, body, token) -> httpx.Response:
        return self._ok(
            {
                "name": self.store.store_name,
                "tagline": TAGLINE,
                "logo_url": f"{STORE_URL}/logo.png",
                "colors": dict(BRAND_COLOURS),
                "currency": "USD",
                "checkout_url": f"{STORE_URL}/checkout/",
                "cart_url": f"{STORE_URL}/cart/",
            }
        )

    def _orders(self, match, params, body, token) -> httpx.Response:
        if not token or (token not in self.carts and token not in self.orders):
            raise RestError(
                "claude_commerce_invalid_cart_token",
                "The Cart-Token header is missing, invalid, or expired.",
                403,
            )
        return self._ok({"orders": list(reversed(self.orders.get(token, [])))})

    # -- Catalog --------------------------------------------------------------------------

    def _published(self, product_id: int) -> dict[str, Any]:
        for node in self.store.products:
            if node["id"] == product_id and node["status"] == "publish":
                return node
        raise RestError("woocommerce_rest_product_invalid_id", "Invalid product ID.", 404)

    def _variation_row(self, variation_id: int) -> tuple[dict[str, Any], int] | None:
        for parent_id, rows in self.store.variations.items():
            for row in rows:
                if row["id"] == variation_id:
                    return row, parent_id
        return None

    def _categories(self, match, params, body, token) -> httpx.Response:
        return self._ok(
            [
                {"id": t["id"], "name": t["name"], "slug": t["slug"], "count": 0}
                for t in self.store.categories
            ]
        )

    def _products(self, match, params, body, token) -> httpx.Response:
        if params.get("type") == "variation":
            parent = self._published(int(params.get("parent") or 0))
            rows = self.store.variations.get(parent["id"], [])
            return self._ok([self._variation_document(row, parent) for row in rows])

        nodes = [n for n in self.store.products if n["status"] == "publish"]
        phrase = (params.get("search") or "").casefold()
        if phrase:
            # WordPress: every non-stopword must appear in the name, content, or SKU.
            words = [w for w in phrase.split() if w not in WP_STOPWORDS] or phrase.split()
            nodes = [
                n
                for n in nodes
                if all(
                    w in n["name"].casefold()
                    or w in n["description"].casefold()
                    or w in (n["sku"] or "").casefold()
                    for w in words
                )
            ]
        if category := params.get("category"):
            nodes = [n for n in nodes if any(str(c["id"]) == category for c in n["categories"])]

        documents = [self._product_document(n) for n in nodes]
        floor, ceiling = params.get("min_price"), params.get("max_price")
        if floor is not None:
            documents = [d for d in documents if int(d["prices"]["price"]) >= int(floor)]
        if ceiling is not None:
            documents = [d for d in documents if int(d["prices"]["price"]) <= int(ceiling)]
        if params.get("orderby") == "price":
            documents.sort(
                key=lambda d: int(d["prices"]["price"]), reverse=params.get("order") == "desc"
            )
        return self._ok(documents[: int(params.get("per_page") or 10)])

    def _product(self, match, params, body, token) -> httpx.Response:
        wanted = int(match.group(1))
        found = self._variation_row(wanted)
        if found:
            row, parent_id = found
            return self._ok(self._variation_document(row, self._published(parent_id)))
        return self._ok(self._product_document(self._published(wanted)))

    def _product_document(self, node: dict[str, Any]) -> dict[str, Any]:
        rows = self.store.variations.get(node["id"], [])
        is_family = node["type"] == "variable"
        cheapest = min(rows, key=lambda r: float(r["price"] or 0)) if is_family and rows else node
        quantity = node.get("stock_quantity") if node.get("manage_stock") else None
        return {
            "id": node["id"],
            "name": node["name"],
            "slug": node["slug"],
            "parent": 0,
            "type": node["type"],
            "variation": "",
            "permalink": f"{STORE_URL}/product/{node['slug']}/",
            "sku": node["sku"],
            "short_description": node["short_description"],
            "description": node["description"],
            "on_sale": bool(node.get("on_sale")),
            "prices": price_block(cheapest),
            "average_rating": "4.50" if node["id"] == 101 else "0",
            "review_count": 2 if node["id"] == 101 else 0,
            "images": [
                {"id": i["id"], "src": i["src"], "thumbnail": i["src"], "alt": ""}
                for i in node["images"]
            ],
            "categories": [
                {"id": c["id"], "name": c["name"], "slug": c["slug"]} for c in node["categories"]
            ],
            "attributes": [
                {
                    "id": 0,
                    "name": a["name"],
                    "taxonomy": None,
                    "has_variations": bool(a.get("variation")),
                    "terms": [{"id": 0, "name": o, "slug": slug_of(o)} for o in a["options"]],
                }
                for a in node.get("attributes") or []
            ],
            "variations": [
                {
                    "id": r["id"],
                    "attributes": [
                        {"name": a["name"], "value": slug_of(a["option"])} for a in r["attributes"]
                    ],
                }
                for r in rows
            ],
            "has_options": is_family,
            "is_purchasable": True,
            "is_in_stock": any(in_stock(r) for r in rows) if is_family else in_stock(node),
            "is_on_backorder": False,
            "low_stock_remaining": quantity if quantity is not None and 0 < quantity <= 5 else None,
        }

    def _variation_document(self, row: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any]:
        suffix = option_suffix(row)
        return {
            "id": row["id"],
            "name": f"{parent['name']} - {suffix}",
            "slug": f"{parent['slug']}-{slug_of(suffix)}",
            "parent": parent["id"],
            "type": "variation",
            "variation": ", ".join(f"{a['name']}: {a['option']}" for a in row["attributes"]),
            "sku": row["sku"],
            "short_description": parent["short_description"],
            "description": parent["description"],
            "on_sale": bool(row.get("on_sale")),
            "prices": price_block(row),
            "average_rating": "0",
            "review_count": 0,
            "images": [{"id": 0, "src": row["image"]["src"], "thumbnail": row["image"]["src"]}]
            if row.get("image")
            else [],
            "categories": [],
            "attributes": [
                {
                    "id": 0,
                    "name": a["name"],
                    "taxonomy": None,
                    "has_variations": False,
                    "terms": [{"id": 0, "name": a["option"], "slug": slug_of(a["option"])}],
                }
                for a in row["attributes"]
            ],
            "variations": [],
            "has_options": False,
            "is_purchasable": True,
            "is_in_stock": in_stock(row),
            "is_on_backorder": False,
            "low_stock_remaining": None,
        }

    # -- Cart -------------------------------------------------------------------------------

    def _cart(self, match, params, body, token) -> httpx.Response:
        if token is None:
            token = self.carts.open()
        elif token not in self.carts:
            raise RestError("woocommerce_rest_invalid_cart_token", "Invalid cart token.", 403)
        return self._ok(self._cart_document(token), **{"Cart-Token": token})

    def _cart_write(self, match, params, body, token) -> httpx.Response:
        action = match.group(1)
        self.carts.require(token)
        if action == "add-item":
            node, parent = self._sellable(int(body["id"]))
            if not in_stock(node):
                raise RestError(
                    "woocommerce_rest_product_out_of_stock",
                    "That product is out of stock and cannot be purchased.",
                    400,
                )
            self.carts.add(token, node, parent, int(body.get("quantity") or 1))
        elif action == "update-item":
            self.carts.set_quantity(token, body.get("key"), int(body["quantity"]))
        elif action == "remove-item":
            self.carts.remove(token, body.get("key"))
        else:
            raise RestError("rest_no_route", "No route.", 404)
        return self._ok(self._cart_document(token), **{"Cart-Token": token})

    def _sellable(self, wanted: int) -> tuple[dict[str, Any], dict[str, Any]]:
        """The node an add names and its parent (itself for a simple product)."""
        found = self._variation_row(wanted)
        if found:
            row, parent_id = found
            return row, self._published(parent_id)
        try:
            node = self._published(wanted)
        except RestError:
            raise RestError(
                "woocommerce_rest_cart_invalid_product",
                "This product cannot be added to the cart.",
                400,
            ) from None
        if node["type"] == "variable":
            raise RestError(
                "woocommerce_rest_cart_product_is_not_purchasable", "Choose a variation.", 400
            )
        return node, node

    def _cart_document(self, token: str) -> dict[str, Any]:
        lines = self.carts[token]
        money = {"currency_code": "USD", "currency_minor_unit": MINOR_UNIT}
        total = sum(float(line["price"]) * line["quantity"] for line in lines)
        items = []
        for line in lines:
            unit = minor(line["price"])
            items.append(
                {
                    "key": line["key"],
                    "id": line["id"],
                    "quantity": line["quantity"],
                    "name": line["name"],
                    "images": [{"id": 0, "src": line["image"], "thumbnail": line["image"]}]
                    if line["image"]
                    else [],
                    "variation": line["variation"],
                    "prices": {"price": unit, "regular_price": unit, "sale_price": unit, **money},
                    "totals": {
                        "line_total": minor(float(line["price"]) * line["quantity"]),
                        **money,
                    },
                }
            )
        return {
            "items": items,
            "totals": {"total_items": minor(total), "total_price": minor(total), **money},
            "needs_shipping": bool(lines),
            "shipping_rates": [SHIPPING_PACKAGE] if lines else [],
        }

    # -- Pages ------------------------------------------------------------------------------

    def _pages(self, match, params, body, token) -> httpx.Response:
        words = [w for w in (params.get("search") or "").casefold().split() if len(w) > 2]
        hits = [
            (page_id, slug, title, text)
            for page_id, slug, title, text in PAGES
            if any(w in title.casefold() or w in text.casefold() for w in words)
        ]
        return self._ok(
            [
                {
                    "id": page_id,
                    "slug": slug,
                    "link": f"{STORE_URL}/{slug}/",
                    "title": {"rendered": title},
                    "content": {"rendered": f"<p>{text}</p>"},
                }
                for page_id, slug, title, text in hits[: int(params.get("per_page") or 10)]
            ]
        )
