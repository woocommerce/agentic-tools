# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""``StorefrontBackend`` for a live WooCommerce site, over the public Store API.

How the pieces fit:

* :class:`DisplayCache` holds every product mapped so far, shared by all sessions. It
  backs the host's ``/api/products`` grid, knows which family a variation belongs to, and
  remembers each family's fallback variation. It is not provenance: the executor decides
  what a session may put in its cart.
* :class:`CartSession` is the per-session record: the Store API cart token and the line
  keys WooCommerce needs for ``update-item`` and ``remove-item``. A token the store stops
  honouring is forgotten, and the next add opens a fresh cart.
* The module-level mappers turn Store API documents (products, variations, carts, bridge
  orders, shipping packages) into the shopping agent's models.
* :class:`PolicyPages` searches the site's published pages, since a shop's returns and
  shipping policies are ordinary WordPress pages.

Ids are WordPress post ids as strings. A variable product is a family whose
``get_product_details`` lists each variation under its own id; cart lines always carry a
variation id. Checkout is a handoff and nothing more: no call here ever reaches the Store
API's ``/checkout`` route. With the bridge plugin active the handoff URL carries the cart
token so the plugin can adopt the cart into the shopper's browser session; without it, a
single-line cart falls back to WooCommerce's ``add-to-cart`` URL and a larger cart has no
handoff. Orders are read only through the bridge, authorised by the token of the cart that
placed them.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import UTC, datetime
from typing import Any, NamedTuple
from urllib.parse import quote

from shopping_agent import (
    Cart,
    CartItem,
    CheckoutHandoff,
    FulfillmentOption,
    Order,
    OrderItem,
    OrderStatus,
    Policy,
    Product,
    ProductDetails,
    SearchFilters,
    ShoppingSessionContext,
    StorefrontBackend,
    Unavailable,
    UserPreferences,
)

from .store_client import (
    BRIDGE_V1,
    STORE_V1,
    WP_V2,
    CartTokenError,
    StoreApiClient,
    StoreApiError,
    cart_token_of,
)

logger = logging.getLogger(__name__)

# Query var the bridge plugin reads to adopt an agent-built cart into the browser session.
CART_HANDOFF_PARAM = "claude_commerce_cart"

POLICY_LIMIT = 5
POLICY_EXCERPT_CHARS = 1500
WIDENED_SEARCH_TERMS = 6
PICKUP_METHODS = frozenset({"local_pickup", "pickup_location"})
PICKUP_ADDRESS_KEYS = frozenset({"pickup_address", "pickup_location"})

# Store API error slugs that mean "exists, but cannot be bought right now".
STOCK_ERROR_SLUGS = frozenset(
    {
        "woocommerce_rest_product_out_of_stock",
        "woocommerce_rest_product_partially_out_of_stock",
        "woocommerce_rest_cart_product_no_stock",
        "woocommerce_rest_cart_product_is_not_purchasable",
        "woocommerce_rest_product_not_purchasable",
    }
)

WOO_ORDER_STATUS: dict[str, OrderStatus] = {
    "completed": OrderStatus.SHIPPED,
    "cancelled": OrderStatus.CANCELLED,
    "failed": OrderStatus.CANCELLED,
    "refunded": OrderStatus.REFUNDED,
}
SORT_PARAMS: dict[str, dict[str, str]] = {
    "price_asc": {"orderby": "price", "order": "asc"},
    "price_desc": {"orderby": "price", "order": "desc"},
    "rating": {"orderby": "rating", "order": "desc"},
}
POLICY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "return": ("refund", "returns"),
    "returns": ("refund", "return"),
    "refund": ("return", "refunds"),
    "exchange": ("return", "refund"),
    "shipping": ("delivery",),
    "delivery": ("shipping",),
    "warranty": ("guarantee",),
    "cancel": ("cancellation",),
}
# Words dropped before a phrase is retried one word at a time against WooCommerce's
# literal search.
SEARCH_FILLER = frozenset(
    {
        "the", "and", "for", "with", "under", "over", "gift", "gifts", "something", "someone",
        "who", "just", "that", "this", "some", "any", "what", "which", "recommend", "looking",
        "want", "need", "good", "best", "cheap", "nice", "present", "idea", "ideas", "about",
        "from", "have", "has", "into", "set", "one",
    }
)  # fmt: skip

_TAGS = re.compile(r"<[^>]*>")
_SPACES = re.compile(r"\s+")
_WORDS = re.compile(r"[a-z0-9]+")


# -- Text and money -------------------------------------------------------------------------


def plain_text(value: Any) -> str | None:
    """Rendered WordPress HTML as one line of text, entities decoded; None when empty."""
    if not isinstance(value, str) or not value:
        return None
    text = _SPACES.sub(" ", html.unescape(_TAGS.sub(" ", value))).strip()
    return text or None


def search_terms(query: str) -> list[str]:
    return [w for w in _WORDS.findall(query.lower()) if len(w) > 2 and w not in SEARCH_FILLER]


class Money(NamedTuple):
    """The currency the store quotes in and how many decimals its minor unit has."""

    currency: str = "USD"
    minor_unit: int = 2

    @classmethod
    def from_block(cls, block: dict[str, Any] | None, fallback: Money) -> Money:
        block = block or {}
        return cls(
            str(block.get("currency_code") or fallback.currency),
            int(block.get("currency_minor_unit") or fallback.minor_unit),
        )

    def amount(self, raw: Any) -> float:
        """A Store API amount string in minor units ("4000") as a decimal number (40.0)."""
        try:
            return round(int(raw) / 10**self.minor_unit, 2)
        except (TypeError, ValueError):
            return 0.0

    def to_minor(self, value: float) -> int:
        return int(round(value * 10**self.minor_unit))


def first_image(record: dict[str, Any], key: str = "src") -> str | None:
    for image in record.get("images") or []:
        if isinstance(image, dict) and image.get(key):
            return str(image[key])
    return None


def first_name(entries: Any) -> str | None:
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("name"):
            return str(entry["name"])
    return None


# -- Store API product documents to shopping_agent models ------------------------------------


def _term_names(attribute: dict[str, Any]) -> list[str]:
    return [
        str(t["name"])
        for t in attribute.get("terms") or []
        if isinstance(t, dict) and t.get("name")
    ]


def product_details_from(record: dict[str, Any], variants: list[Product]) -> ProductDetails:
    """A Store API product (family or simple) as ``ProductDetails``. ``variants`` are the
    already-mapped variations; a family's price and stock are derived from them."""
    product_id = str(record["id"])
    prices = record.get("prices") or {}
    money = Money.from_block(prices, Money())
    price = money.amount(prices.get("price"))
    is_family = record.get("type") == "variable"

    options: dict[str, list[str]] = {}
    attributes: dict[str, str] = {}
    for attribute in record.get("attributes") or []:
        if not isinstance(attribute, dict) or not attribute.get("name"):
            continue
        names = _term_names(attribute)
        if attribute.get("has_variations"):
            if is_family:
                options[str(attribute["name"])] = names
        elif names:
            attributes[str(attribute["name"])] = ", ".join(names)
    if record.get("sku"):
        attributes["sku"] = str(record["sku"])

    labels: list[str] = []
    if record.get("on_sale"):
        labels.append("on sale")
        regular = money.amount(prices.get("regular_price")) if prices.get("regular_price") else None
        if regular is not None and regular > price:
            attributes["regular_price"] = f"{regular:g}"
    if record.get("is_on_backorder"):
        labels.append("backorder")
    if record.get("low_stock_remaining"):
        labels.append(f"only {record['low_stock_remaining']} left")

    reviews = int(record.get("review_count") or 0)
    long_description = plain_text(record.get("description"))
    short_description = plain_text(record.get("short_description"))
    if short_description is None and long_description:
        short_description = long_description[:200]
    sellable = [variant for variant in variants if variant.in_stock]

    return ProductDetails(
        product_id=product_id,
        title=plain_text(record.get("name")) or product_id,
        brand=first_name(record.get("brands")),
        price=min((variant.price for variant in sellable), default=price) if variants else price,
        currency=money.currency,
        rating=float(record["average_rating"])
        if reviews and record.get("average_rating")
        else None,
        review_count=reviews or None,
        image_url=first_image(record),
        category=first_name(record.get("categories")),
        labels=labels,
        attributes=attributes,
        in_stock=bool(sellable) if variants else bool(record.get("is_in_stock")),
        short_description=short_description,
        long_description=long_description,
        options=options,
        variants=variants,
    )


def family_option_values(family: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Per variation id, the attribute name to term *name* pairs a family record lists.
    The family's ``variations`` entries carry term slugs; its ``attributes`` carry the
    slug-to-name mapping."""
    names: dict[str, str] = {}
    for attribute in family.get("attributes") or []:
        for term in (attribute.get("terms") or []) if isinstance(attribute, dict) else []:
            if isinstance(term, dict) and term.get("slug") and term.get("name"):
                names[str(term["slug"])] = str(term["name"])
    listed: dict[str, dict[str, str]] = {}
    for entry in family.get("variations") or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        pairs = {}
        for pair in entry.get("attributes") or []:
            if isinstance(pair, dict) and pair.get("name"):
                slug = str(pair.get("value"))
                pairs[str(pair["name"])] = names.get(slug, slug)
        listed[str(entry["id"])] = pairs
    return listed


def own_option_values(row: dict[str, Any]) -> dict[str, str]:
    """A variation's option values from its own document: one term per attribute, or the
    ``variation`` string WooCommerce renders ("Size: Large, Color: Blue")."""
    values: dict[str, str] = {}
    for attribute in row.get("attributes") or []:
        if isinstance(attribute, dict) and attribute.get("name"):
            names = _term_names(attribute)
            if names:
                values[str(attribute["name"])] = names[0]
    if not values:
        for chunk in str(row.get("variation") or "").split(","):
            label, sep, value = chunk.partition(":")
            if sep:
                values[label.strip()] = value.strip()
    return values


def variation_from(
    row: dict[str, Any], family: dict[str, Any], option_values: dict[str, str]
) -> Product:
    variation_id = str(row["id"])
    money = Money.from_block(row.get("prices"), Money())
    if option_values:
        title = f"{family.get('name')} — {', '.join(option_values.values())}"
    else:
        title = plain_text(row.get("name")) or variation_id
    return Product(
        product_id=variation_id,
        title=title,
        price=money.amount((row.get("prices") or {}).get("price")),
        currency=money.currency,
        image_url=first_image(row) or first_image(family),
        in_stock=bool(row.get("is_in_stock")) and bool(row.get("is_purchasable", True)),
        option_values=option_values,
        variant_of=str(family["id"]),
        attributes={"sku": str(row["sku"])} if row.get("sku") else {},
    )


def summary_of(details: ProductDetails) -> Product:
    """The search-result view of a details record: everything but the long fields."""
    return Product.model_validate(
        details.model_dump(exclude={"long_description", "specs", "review_highlights", "variants"})
    )


def relevance(record: dict[str, Any], terms: list[str]) -> int:
    """How many query words a Store API product mentions; the name counts double."""
    name = str(record.get("name") or "").lower()
    body = " ".join(
        [
            str(record.get("short_description") or ""),
            str(record.get("description") or ""),
            *(
                str(c.get("name") or "")
                for c in record.get("categories") or []
                if isinstance(c, dict)
            ),
        ]
    ).lower()
    score = 0
    for term in terms:
        if term in name:
            score += 2
        if term in body:
            score += 1
    return score


# -- Bridge orders and shipping packages -----------------------------------------------------


def order_from_bridge(record: dict[str, Any], currency: str) -> Order:
    """A ``wc/v3``-shaped order as the bridge returns it."""
    raw_date = record.get("date_created_gmt") or record.get("date_created")
    try:
        placed_at = datetime.fromisoformat(str(raw_date))
    except (TypeError, ValueError):
        placed_at = datetime.now(UTC)
    if placed_at.tzinfo is None:
        placed_at = placed_at.replace(tzinfo=UTC)

    items: list[OrderItem] = []
    for line in record.get("line_items") or []:
        if not isinstance(line, dict):
            continue
        quantity = max(1, int(line.get("quantity") or 1))
        variation_id = line.get("variation_id")
        item_id = str(variation_id or line.get("product_id") or "")
        items.append(
            OrderItem(
                product_id=item_id,
                title=plain_text(line.get("name")) or item_id,
                quantity=quantity,
                price=round(float(line.get("total") or 0) / quantity, 2),
                variant_of=str(line["product_id"]) if variation_id else None,
            )
        )
    try:
        total = float(record.get("total") or 0)
    except (TypeError, ValueError):
        total = 0.0
    return Order(
        order_id=f"#{record.get('number') or record.get('id')}",
        status=WOO_ORDER_STATUS.get(str(record.get("status")), OrderStatus.PROCESSING),
        currency=str(record.get("currency") or currency),
        total=total,
        tracking_url=record.get("tracking_url") or None,
        placed_at=placed_at,
        items=items,
    )


def fulfillment_from_cart(cart: dict[str, Any], money: Money) -> list[FulfillmentOption]:
    """The shipping and pickup rates a Store API cart document quotes, one option per rate
    across every shipping package."""
    options: list[FulfillmentOption] = []
    for package in cart.get("shipping_rates") or []:
        rates = package.get("shipping_rates") if isinstance(package, dict) else None
        for rate in rates or []:
            if not isinstance(rate, dict):
                continue
            method_id = str(rate.get("method_id") or "")
            address = None
            if method_id in PICKUP_METHODS:
                for meta in rate.get("meta_data") or []:
                    if isinstance(meta, dict) and meta.get("key") in PICKUP_ADDRESS_KEYS:
                        address = str(meta.get("value"))
            options.append(
                FulfillmentOption(
                    method="pickup" if method_id in PICKUP_METHODS else "shipping",
                    eta=str(rate.get("delivery_time") or rate.get("name") or method_id),
                    fee=money.amount(rate.get("price")),
                    location=address,
                )
            )
    return options


# -- Shared display cache -------------------------------------------------------------------


class DisplayCache:
    """Products mapped so far, shared across sessions. Serves the host's product grid and
    resolves variation ids to their family; it grants no provenance."""

    def __init__(self) -> None:
        self.products: dict[str, ProductDetails] = {}
        self._family_of: dict[str, str] = {}
        self._variation_image: dict[str, str] = {}
        self._fallback_variation: dict[str, str] = {}

    def clear(self) -> None:
        self.products.clear()
        self._family_of.clear()
        self._variation_image.clear()
        self._fallback_variation.clear()

    def store(self, details: ProductDetails) -> ProductDetails:
        family_id = details.product_id
        preferred: str | None = None
        for variation in details.variants:
            self._family_of[variation.product_id] = family_id
            if variation.image_url:
                self._variation_image[variation.product_id] = variation.image_url
            if preferred is None or (variation.in_stock and not self._in_stock(preferred, details)):
                preferred = variation.product_id
        if preferred is not None:
            self._fallback_variation[family_id] = preferred
        self.products[family_id] = details
        return details

    @staticmethod
    def _in_stock(variation_id: str, family: ProductDetails) -> bool:
        return any(v.product_id == variation_id and v.in_stock for v in family.variants)

    def get(self, product_id: str) -> ProductDetails | None:
        return self.products.get(product_id)

    def family_of(self, product_id: str) -> str | None:
        return self._family_of.get(product_id)

    def image_of(self, variation_id: str) -> str | None:
        return self._variation_image.get(variation_id)

    def fallback_variation(self, family_id: str) -> str | None:
        return self._fallback_variation.get(family_id)

    def link_variation(self, variation_id: str, family_id: str) -> None:
        self._family_of[variation_id] = family_id

    def variation_details(self, family: ProductDetails, variation_id: str) -> ProductDetails | None:
        """One variation of ``family`` as its own details record."""
        for variation in family.variants:
            if variation.product_id == variation_id:
                fields = variation.model_dump() | {"long_description": family.long_description}
                return ProductDetails.model_validate(fields)
        return None

    def lookup(self, product_id: str) -> ProductDetails | None:
        """A product by id, or a variation found inside its cached family."""
        if product_id in self.products:
            return self.products[product_id]
        family_id = self._family_of.get(product_id)
        family = self.products.get(family_id) if family_id else None
        return self.variation_details(family, product_id) if family else None

    def in_stock_siblings(self, variation_id: str) -> list[str]:
        family_id = self._family_of.get(variation_id)
        family = self.products.get(family_id) if family_id else None
        if family is None:
            return []
        return [
            v.product_id for v in family.variants if v.in_stock and v.product_id != variation_id
        ]


# -- Per-session cart ----------------------------------------------------------------------


class CartLine(NamedTuple):
    key: str
    quantity: int


class CartSession:
    """One shopper's Store API cart: the token that names it and the line keys inside it."""

    __slots__ = ("lines", "token")

    def __init__(self) -> None:
        self.token: str | None = None
        self.lines: dict[str, CartLine] = {}

    def forget(self) -> None:
        self.token = None
        self.lines.clear()

    def adopt_token(self, headers: dict[str, str]) -> None:
        issued = cart_token_of(headers)
        if issued:
            self.token = issued


class WooStorefrontBackend(StorefrontBackend):
    """The shopping agent's view of one WooCommerce site."""

    def __init__(self, client: StoreApiClient, store_name: str | None = None) -> None:
        self.client = client
        self.store_name = store_name or client.store_url
        self._name_pinned = store_name is not None
        self.catalog = DisplayCache()
        self.policies = PolicyPages(client)
        self.money = Money()
        # None until probe() has run; True once the site advertised the bridge namespace.
        self.bridge_available: bool | None = None
        self.checkout_url = f"{client.store_url}/checkout/"
        self.cart_url = f"{client.store_url}/cart/"
        self._carts: dict[str, CartSession] = {}
        self._category_ids: dict[str, int] | None = None

    # -- Site --------------------------------------------------------------------------

    async def probe(self) -> dict[str, Any]:
        """Read the root index for the site's name and the bridge namespace; ask the bridge
        for its checkout and cart URLs. A site that cannot be reached leaves the bridge
        marked absent and startup carries on."""
        try:
            root, _ = await self.client.get("")
        except Exception as failure:  # noqa: BLE001 - a dead site must not stop the host
            logger.warning("no answer from %s/wp-json/: %s", self.client.store_url, failure)
            self.bridge_available = False
            return {}
        root = root if isinstance(root, dict) else {}
        self.bridge_available = BRIDGE_V1 in (root.get("namespaces") or [])
        if root.get("name") and not self._name_pinned:
            self.store_name = str(root["name"])
        if self.bridge_available:
            try:
                info, _ = await self.client.get(f"{BRIDGE_V1}/brand")
            except StoreApiError:
                info = None
            if isinstance(info, dict):
                self.checkout_url = info.get("checkout_url") or self.checkout_url
                self.cart_url = info.get("cart_url") or self.cart_url
        return root

    @property
    def products(self) -> dict[str, ProductDetails]:
        """The display cache's products, for the host's ``/api/products`` routes."""
        return self.catalog.products

    def product(self, product_id: str) -> ProductDetails | None:
        return self.catalog.lookup(product_id)

    def warm_display_cache(self, details: ProductDetails) -> None:
        """Seed the display cache (the startup warm-up's path). No session learns anything."""
        self.catalog.store(details)

    def recent_orders(self, limit: int = 6) -> list[Order]:
        return []  # guests have no order history to show on the host's orders page

    # -- Sessions ----------------------------------------------------------------------

    def _cart(self, session_id: str) -> CartSession:
        return self._carts.setdefault(session_id, CartSession())

    def reset_session(self, session_id: str) -> None:
        self._carts.pop(session_id, None)

    def cart_token_for(self, session_id: str) -> str | None:
        cart = self._carts.get(session_id)
        return cart.token if cart else None

    # -- Catalog -----------------------------------------------------------------------

    async def search_products(
        self,
        session: ShoppingSessionContext,
        query: str,
        filters: SearchFilters | None = None,
        limit: int = 8,
    ) -> list[Product]:
        """WooCommerce's search is a literal match over name, content, and SKU, so a phrase
        like "woodworking gift" usually finds nothing. Three passes: the phrase as typed,
        then each meaningful word on its own, then, if a price or category filter is set,
        the popular products inside that filter. Results are ranked by how many query
        words they mention unless the caller asked for a price or rating order."""
        params, filtered = await self._filter_params(filters, limit)
        terms = search_terms(query)
        phrase = query.strip()

        records = await self._list_products(params, phrase or None)
        if not records and len(terms) > 1:
            by_id: dict[str, dict[str, Any]] = {}
            for term in terms[:WIDENED_SEARCH_TERMS]:
                for record in await self._list_products(params, term):
                    by_id.setdefault(str(record.get("id")), record)
            records = list(by_id.values())
        if not records and filtered and phrase:
            records = await self._list_products(
                {**params, "orderby": "popularity", "order": "desc"}
            )
        if terms and (filters is None or filters.sort == "relevance"):
            records.sort(key=lambda record: relevance(record, terms), reverse=True)

        results: list[Product] = []
        for record in records[:limit]:
            details = self._remember(record, variants=None)
            if (
                filters
                and filters.min_rating is not None
                and (details.rating or 0) < filters.min_rating
            ):
                continue
            results.append(summary_of(details))
        return results

    async def _filter_params(
        self, filters: SearchFilters | None, limit: int
    ) -> tuple[dict[str, Any], bool]:
        """Store API query parameters for the filters; the flag says whether any narrowed
        the catalog (a sort alone does not)."""
        params: dict[str, Any] = {"per_page": limit}
        narrowed = False
        if filters is None:
            return params, narrowed
        if filters.min_price is not None:
            params["min_price"] = self.money.to_minor(filters.min_price)
            narrowed = True
        if filters.max_price is not None:
            params["max_price"] = self.money.to_minor(filters.max_price)
            narrowed = True
        if filters.category:
            term_id = await self._category_id(filters.category)
            if term_id is not None:
                params["category"] = term_id
                narrowed = True
        params.update(SORT_PARAMS.get(filters.sort, {}))
        return params, narrowed

    async def _category_id(self, name: str) -> int | None:
        if self._category_ids is None:
            self._category_ids = {}
            try:
                terms, _ = await self.client.get(
                    f"{STORE_V1}/products/categories", {"per_page": 100}
                )
            except StoreApiError:
                terms = []
            for term in terms if isinstance(terms, list) else []:
                if isinstance(term, dict) and term.get("id"):
                    for label in (term.get("name"), term.get("slug")):
                        if label:
                            self._category_ids[str(label).casefold()] = int(term["id"])
        wanted = name.strip().casefold()
        if wanted in self._category_ids:
            return self._category_ids[wanted]
        return next((tid for label, tid in self._category_ids.items() if wanted in label), None)

    async def _list_products(
        self, params: dict[str, Any], search: str | None = None
    ) -> list[dict[str, Any]]:
        query = dict(params)
        if search:
            query["search"] = search
        body, _ = await self.client.get(f"{STORE_V1}/products", query)
        return [r for r in body if isinstance(r, dict)] if isinstance(body, list) else []

    async def _fetch_record(self, product_id: str) -> dict[str, Any] | None:
        try:
            record, _ = await self.client.get(f"{STORE_V1}/products/{quote(product_id, safe='')}")
        except StoreApiError as error:
            if error.not_found:
                return None
            raise
        return record if isinstance(record, dict) else None

    async def get_product_details(
        self, session: ShoppingSessionContext, product_id: str
    ) -> ProductDetails | None:
        family_id = self.catalog.family_of(product_id)
        if family_id is None:
            record = await self._fetch_record(product_id)
            if record is None:
                return None
            if record.get("type") != "variation" or not record.get("parent"):
                return await self._remember_with_variations(record)
            family_id = str(record["parent"])
            self.catalog.link_variation(product_id, family_id)
        family = self.catalog.get(family_id)
        if family is None or not family.variants:
            record = await self._fetch_record(family_id)
            if record is None:
                return None
            family = await self._remember_with_variations(record)
        return self.catalog.variation_details(family, product_id)

    async def _remember_with_variations(self, record: dict[str, Any]) -> ProductDetails:
        variants: list[Product] = []
        if record.get("type") == "variable":
            rows, _ = await self.client.get(
                f"{STORE_V1}/products",
                {"type": "variation", "parent": str(record["id"]), "per_page": 100},
            )
            listed = family_option_values(record)
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict) and row.get("id"):
                    values = listed.get(str(row["id"])) or own_option_values(row)
                    variants.append(variation_from(row, record, values))
        return self._remember(record, variants=variants)

    def _remember(
        self, record: dict[str, Any], *, variants: list[Product] | None
    ) -> ProductDetails:
        """Map and cache a product record. ``variants=None`` means the variations were not
        fetched this time (a search result), so a fuller list already cached survives."""
        self.money = Money.from_block(record.get("prices"), self.money)
        if variants is None:
            cached = self.catalog.get(str(record["id"]))
            variants = cached.variants if cached else []
        return self.catalog.store(product_details_from(record, variants))

    # -- Cart ----------------------------------------------------------------------------

    def _empty_cart(self) -> Cart:
        return Cart(currency=self.money.currency)

    def _absorb_cart(
        self, cart: CartSession, payload: dict[str, Any], headers: dict[str, str]
    ) -> Cart:
        """Read a Store API cart document into the session (token, line keys) and return it
        as a ``Cart``. Line prices are in minor units; ``variation`` lists the chosen
        attribute values."""
        cart.adopt_token(headers)
        self.money = Money.from_block(payload.get("totals"), self.money)
        cart.lines.clear()
        items: list[CartItem] = []
        for line in payload.get("items") or []:
            if not isinstance(line, dict):
                continue
            item_id = str(line.get("id"))
            quantity = int(line.get("quantity") or 0)
            cart.lines[item_id] = CartLine(str(line.get("key")), quantity)
            line_money = Money.from_block(line.get("prices"), self.money)
            items.append(
                CartItem(
                    product_id=item_id,
                    title=plain_text(line.get("name")) or item_id,
                    price=line_money.amount((line.get("prices") or {}).get("price")),
                    quantity=max(1, quantity),
                    image_url=first_image(line, "thumbnail")
                    or first_image(line)
                    or self.catalog.image_of(item_id),
                    option_values={
                        str(pair["attribute"]): str(pair.get("value"))
                        for pair in line.get("variation") or []
                        if isinstance(pair, dict) and pair.get("attribute")
                    },
                    variant_of=self.catalog.family_of(item_id),
                )
            )
        return Cart(items=items, currency=self.money.currency)

    async def get_cart(self, session: ShoppingSessionContext) -> Cart:
        cart = self._cart(session.session_id)
        if cart.token is None:
            return self._empty_cart()
        try:
            payload, headers = await self.client.get(f"{STORE_V1}/cart", cart_token=cart.token)
        except CartTokenError:
            cart.forget()
            return self._empty_cart()
        return self._absorb_cart(cart, payload, headers)

    async def attach_cart(self, session_id: str, cart_token: str) -> Cart | None:
        """Point ``session_id`` at a cart the page already holds; None when the store does
        not recognise the token."""
        try:
            payload, headers = await self.client.get(f"{STORE_V1}/cart", cart_token=cart_token)
        except CartTokenError:
            return None
        cart = self._cart(session_id)
        cart.token = cart_token
        return self._absorb_cart(cart, payload, headers)

    async def _open_cart(self, cart: CartSession) -> None:
        """Cart writes need a Cart-Token, and WooCommerce issues one on the first cart read;
        a session without a token reads its (empty) cart to get one."""
        if cart.token is not None:
            return
        payload, headers = await self.client.get(f"{STORE_V1}/cart")
        cart.adopt_token(headers)
        if cart.token is None:
            raise StoreApiError("the Store API answered the cart read without a Cart-Token")
        self._absorb_cart(cart, payload, headers)

    async def _write_cart(
        self, cart: CartSession, action: str, body: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """POST one ``cart/<action>``. A rejected token is forgotten; an add is then retried
        into a fresh cart, while an update or removal has nothing left to change."""
        await self._open_cart(cart)
        try:
            return await self.client.post(f"{STORE_V1}/cart/{action}", body, cart_token=cart.token)
        except CartTokenError:
            cart.forget()
            if action != "add-item":
                raise
        await self._open_cart(cart)
        return await self.client.post(f"{STORE_V1}/cart/{action}", body, cart_token=cart.token)

    async def add_to_cart(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        cart = self._cart(session.session_id)
        item_id = await self._purchasable_id(session, product_id)
        try:
            payload, headers = await self._write_cart(
                cart, "add-item", {"id": int(item_id), "quantity": quantity}
            )
        except StoreApiError as error:
            if error.code in STOCK_ERROR_SLUGS or error.status == 400:
                raise Unavailable(self._stock_message(item_id)) from error
            raise
        return self._absorb_cart(cart, payload, headers)

    async def update_cart_item(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        return await self._change_line(session, product_id, quantity=quantity)

    async def remove_from_cart(self, session: ShoppingSessionContext, product_id: str) -> Cart:
        return await self._change_line(session, product_id, quantity=0)

    async def _change_line(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        """Set a line's quantity (0 removes it). The cart is re-read first, since the page
        that shares the token may have changed it; a line the cart lacks is left alone."""
        cart = self._cart(session.session_id)
        current = await self.get_cart(session)
        item_id = product_id
        if item_id not in cart.lines:
            item_id = self.catalog.fallback_variation(product_id) or product_id
        line = cart.lines.get(item_id)
        if cart.token is None or line is None:
            return current
        if quantity > 0:
            action, body = "update-item", {"key": line.key, "quantity": quantity}
        else:
            action, body = "remove-item", {"key": line.key}
        try:
            payload, headers = await self._write_cart(cart, action, body)
        except CartTokenError:
            return self._empty_cart()
        except StoreApiError as error:
            if quantity > 0 and (error.code in STOCK_ERROR_SLUGS or error.status == 400):
                raise Unavailable(self._stock_message(item_id)) from error
            raise
        return self._absorb_cart(cart, payload, headers)

    async def _purchasable_id(self, session: ShoppingSessionContext, product_id: str) -> str:
        """The id WooCommerce will accept on ``add-item``: a variation or simple product as
        given, a family replaced by its fallback variation. The executor already stops the
        model from adding a family; this serves the web app's direct add button."""
        if self.catalog.family_of(product_id) is not None:
            return product_id
        details = self.catalog.get(product_id)
        if details is None:
            details = await self.get_product_details(session, product_id)
        if details is None:
            raise Unavailable(f"{product_id} is not in the catalog")
        if not details.options:
            return product_id
        fallback = self.catalog.fallback_variation(product_id)
        if fallback is None:
            raise Unavailable(f"{product_id} has no purchasable variation")
        return fallback

    def _stock_message(self, item_id: str) -> str:
        """Ids only, as the executor expects: the item, then any in-stock siblings."""
        message = f"{item_id} is out of stock or cannot be bought right now"
        siblings = self.catalog.in_stock_siblings(item_id)
        if siblings:
            message += "; in stock: " + ", ".join(siblings)
        return message

    # -- Checkout handoff: a link, never a call to /checkout ------------------------------

    def checkout_url_for(self, session_id: str) -> str | None:
        cart = self._carts.get(session_id)
        if cart is None or cart.token is None or not cart.lines:
            return None
        if self.bridge_available:
            return f"{self.client.store_url}/?{CART_HANDOFF_PARAM}={quote(cart.token, safe='')}"
        if len(cart.lines) == 1:
            ((item_id, line),) = cart.lines.items()
            return f"{self.checkout_url}?add-to-cart={item_id}&quantity={line.quantity}"
        return None

    async def checkout_handoff(
        self, session: ShoppingSessionContext, cart: Cart
    ) -> list[CheckoutHandoff]:
        url = self.checkout_url_for(session.session_id)
        if url is None:
            return []
        return [CheckoutHandoff(url=url, label=f"Check out at {self.store_name}")]

    # -- Shopper, orders, policies, shipping ----------------------------------------------

    async def get_preferences(self, session: ShoppingSessionContext) -> UserPreferences:
        return UserPreferences(display_name="Guest", user_id=session.user_id)

    async def get_orders(self, session: ShoppingSessionContext, limit: int = 5) -> list[Order]:
        """Orders placed from this session's cart, via the bridge, with the cart token as
        the credential. No bridge or no cart means nothing to ask for; a guest has no
        identity that would justify reading anything else."""
        cart = self._cart(session.session_id)
        if not self.bridge_available or cart.token is None:
            return []
        try:
            body, _ = await self.client.get(f"{BRIDGE_V1}/orders", cart_token=cart.token)
        except StoreApiError as error:
            logger.info("bridge declined the orders read: %s", error)
            return []
        records = body.get("orders") if isinstance(body, dict) else body
        orders = [
            order_from_bridge(r, self.money.currency) for r in records or [] if isinstance(r, dict)
        ]
        orders.sort(key=lambda order: order.placed_at, reverse=True)
        return orders[:limit]

    async def get_order(self, session: ShoppingSessionContext, order_id: str) -> Order | None:
        wanted = order_id.strip().lstrip("#")
        orders = await self.get_orders(session, limit=20)
        return next((o for o in orders if o.order_id.lstrip("#") == wanted), None)

    async def search_policies(self, session: ShoppingSessionContext, query: str) -> list[Policy]:
        return await self.policies.search(query)

    async def get_fulfillment_options(
        self, session: ShoppingSessionContext, product_ids: list[str]
    ) -> list[FulfillmentOption]:
        """Rates WooCommerce quotes for these products together, at its default customer
        location, computed in a scratch cart that is left to expire. The session's own
        cart is untouched."""
        payload, headers = await self.client.get(f"{STORE_V1}/cart")
        scratch_token = cart_token_of(headers)
        if scratch_token is None:
            return []
        filled = False
        for product_id in product_ids:
            try:
                item_id = await self._purchasable_id(session, product_id)
                payload, _ = await self.client.post(
                    f"{STORE_V1}/cart/add-item",
                    {"id": int(item_id), "quantity": 1},
                    cart_token=scratch_token,
                )
                filled = True
            except (StoreApiError, Unavailable, ValueError):
                continue
        if not filled or not isinstance(payload, dict):
            return []
        return fulfillment_from_cart(payload, Money.from_block(payload.get("totals"), self.money))


class PolicyPages:
    """Policy search over the site's published pages. Terms that miss are retried with
    their synonyms (return/refund, shipping/delivery, ...) until enough pages are found."""

    def __init__(self, client: StoreApiClient) -> None:
        self.client = client

    @staticmethod
    def queries_for(query: str) -> list[str]:
        variants = [query]
        for word in _WORDS.findall(query.lower()):
            if len(word) > 2:
                variants.extend(POLICY_SYNONYMS.get(word, ()))
        return list(dict.fromkeys(variants))

    async def search(self, query: str) -> list[Policy]:
        found: dict[str, Policy] = {}
        for text in self.queries_for(query):
            if len(found) >= POLICY_LIMIT:
                break
            try:
                pages, _ = await self.client.get(
                    f"{WP_V2}/pages",
                    {
                        "search": text,
                        "per_page": POLICY_LIMIT,
                        "_fields": "id,slug,title,content,link",
                    },
                )
            except StoreApiError:
                continue
            for page in pages if isinstance(pages, list) else []:
                policy = self._policy_from(page)
                if policy is not None:
                    found.setdefault(str(page["id"]), policy)
        return list(found.values())[:POLICY_LIMIT]

    @staticmethod
    def _policy_from(page: Any) -> Policy | None:
        if not isinstance(page, dict) or not page.get("id"):
            return None
        content = plain_text((page.get("content") or {}).get("rendered"))
        if not content:
            return None
        slug = str(page.get("slug") or page["id"])
        return Policy(
            policy_id=slug,
            title=plain_text((page.get("title") or {}).get("rendered")) or slug,
            category="page",
            content=content[:POLICY_EXCERPT_CHARS],
        )
