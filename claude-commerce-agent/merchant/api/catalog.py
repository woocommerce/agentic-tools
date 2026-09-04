# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""Product data: ``wc/v3`` product and variation records, the in-process cache that serves
the portal and the alert rules, and the conversion to the interface's ``Listing`` and
``ListingDetails``.

Listing ids are WordPress post ids as strings, unchanged in both directions, because the
same id has to work when a write comes back through ``staging.py``. WooCommerce assigns
products and variations from one post id sequence, so no prefix is needed to tell them
apart. Status mapping: ``draft``, ``pending``, and ``private`` (all hidden from the shop)
become ``paused``, matching what the pause action writes; ``trash`` is filtered out while
loading.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from merchant_agent import Listing, ListingDetails

from .rest_client import Routes, WooApiError, WooExecutor, paged

logger = logging.getLogger(__name__)

SUMMARY_CHARS = 160
THIN_DESCRIPTION_CHARS = 80
HIDDEN_STATUSES = frozenset({"draft", "pending", "private"})
SELLABLE_STOCK_STATUSES = frozenset({"instock", "onbackorder"})
COST_META_KEYS = frozenset({"_wc_cog_cost", "_cost"})

# Words that carry no product meaning in a search phrase, including the generic nouns the
# agent tends to add ("show me the products with ...").
_NOISE_WORDS = frozenset(
    {
        "a", "an", "and", "any", "are", "for", "from", "how", "in", "is", "it", "me", "my",
        "of", "on", "or", "our", "show", "that", "the", "this", "to", "with", "what",
        "listing", "listings", "product", "products", "item", "items",
    }
)  # fmt: skip
_WHOLE_CATALOG_WORDS = frozenset(
    {"all", "everything", "catalog", "catalogue", "store", "inventory"}
)
_TOKEN = re.compile(r"[A-Za-z0-9']+")
_MARKUP = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")

# Search weights, in the order a term is tried against a record: the first field that
# contains the term supplies its weight.
_TITLE_WEIGHT = 4
_SKU_WEIGHT = 3
_CATEGORY_WEIGHT = 2
_TEXT_WEIGHT = 1


# -- Reading wc/v3 JSON --------------------------------------------------------------------


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def strip_html(value: Any) -> str | None:
    """Plain text from a WordPress HTML field, whitespace collapsed; None when empty."""
    if not isinstance(value, str) or not value:
        return None
    text = _SPACE.sub(" ", _MARKUP.sub(" ", value)).strip()
    return text or None


def unit_cost_of(node: dict[str, Any]) -> float | None:
    """Per-unit cost for a product or variation node, or None. Checked in order: the
    ``cost_of_goods_sold`` block WooCommerce core exposes once a store enables it, then the
    ``_wc_cog_cost`` / ``_cost`` meta keys written by the Cost of Goods extension. With
    neither present, no margin is computed anywhere downstream."""
    for candidate in _cost_candidates(node):
        amount = _float_or_none(candidate)
        if amount is not None and amount > 0:
            return amount
    return None


def _cost_candidates(node: dict[str, Any]) -> Iterator[Any]:
    cogs = node.get("cost_of_goods_sold")
    if isinstance(cogs, dict):
        yield cogs.get("total_value")
        for entry in cogs.get("values") or []:
            if isinstance(entry, dict):
                yield entry.get("defined_value")
    for meta in node.get("meta_data") or []:
        if isinstance(meta, dict) and meta.get("key") in COST_META_KEYS:
            yield meta.get("value")


class _Node:
    """Typed accessors over one ``wc/v3`` JSON object, so the record builders read
    ``node.text("name")`` instead of repeating the ``or`` / ``str()`` dance per field."""

    __slots__ = ("raw",)

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw

    def text(self, key: str, default: str = "") -> str:
        value = self.raw.get(key)
        return str(value) if value not in (None, "") else default

    def optional_text(self, key: str) -> str | None:
        return self.text(key) or None

    def flag(self, key: str) -> bool:
        return bool(self.raw.get(key))

    def number(self, key: str) -> float | None:
        return _float_or_none(self.raw.get(key))

    def count(self, key: str) -> int | None:
        return _int_or_none(self.raw.get(key))

    def rows(self, key: str) -> list[dict[str, Any]]:
        return [row for row in self.raw.get(key) or [] if isinstance(row, dict)]

    def modified(self) -> str | None:
        return self.raw.get("date_modified_gmt") or self.raw.get("date_modified")

    def image_src(self) -> str | None:
        image = self.raw.get("image")
        return image.get("src") if isinstance(image, dict) else None

    def prices(self) -> tuple[float, float]:
        """``(regular_price, price)`` with each filling in for the other when absent, and
        0.0 when the node carries neither (a variable parent, for instance)."""
        regular, current = self.number("regular_price"), self.number("price")
        if regular is None:
            regular = current or 0.0
        if current is None:
            current = regular
        return regular, current


@dataclass(frozen=True)
class _StockLevel:
    """Stock arithmetic shared by products and variations. WooCommerce only holds a count
    when ``manage_stock`` is on; otherwise ``stock_status`` is the whole story."""

    managed: bool
    quantity: int | None
    status: str

    @property
    def counted(self) -> bool:
        return self.managed and self.quantity is not None

    @property
    def units(self) -> int:
        return max(0, self.quantity or 0) if self.counted else 0

    @property
    def available(self) -> bool:
        if self.counted:
            return (self.quantity or 0) > 0 or self.status == "onbackorder"
        return self.status in SELLABLE_STOCK_STATUSES


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# -- Records -------------------------------------------------------------------------------


@dataclass(frozen=True)
class VariantRecord:
    """A ``wc/v3/products/{id}/variations`` row, with option names taken from its
    ``attributes`` entries."""

    variant_id: str
    parent_id: str
    sku: str | None
    status: str
    regular_price: float
    price: float
    sale_price: float | None
    on_sale: bool
    manage_stock: bool
    stock_quantity: int | None
    stock_status: str
    option_values: dict[str, str]
    image_url: str | None
    unit_cost: float | None
    updated_at: str | None

    @classmethod
    def from_node(cls, raw: dict[str, Any], parent_id: str) -> VariantRecord:
        node = _Node(raw)
        regular, current = node.prices()
        return cls(
            variant_id=node.text("id"),
            parent_id=parent_id,
            sku=node.optional_text("sku"),
            status=node.text("status", "publish"),
            regular_price=regular,
            price=current,
            sale_price=node.number("sale_price"),
            on_sale=node.flag("on_sale"),
            manage_stock=node.flag("manage_stock"),
            stock_quantity=node.count("stock_quantity"),
            stock_status=node.text("stock_status", "instock"),
            option_values=_option_values(node.rows("attributes")),
            image_url=node.image_src(),
            unit_cost=unit_cost_of(raw),
            updated_at=node.modified(),
        )

    @property
    def _level(self) -> _StockLevel:
        return _StockLevel(self.manage_stock, self.stock_quantity, self.stock_status)

    @property
    def title_suffix(self) -> str:
        return ", ".join(self.option_values.values())

    @property
    def stock(self) -> int:
        return self._level.units

    @property
    def in_stock(self) -> bool:
        return self._level.available


def _option_values(attributes: list[dict[str, Any]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for entry in attributes:
        name, option = entry.get("name"), entry.get("option")
        if name and option:
            values[str(name)] = str(option)
    return values


def _variation_options(attributes: list[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    return {
        str(entry["name"]): tuple(str(option) for option in entry.get("options") or [])
        for entry in attributes
        if entry.get("variation") and entry.get("name")
    }


@dataclass(frozen=True)
class ProductRecord:
    """A ``wc/v3/products`` row as the cache holds it. ``product_id`` is the post id as a
    string; ``variants`` is populated only when ``type`` is ``variable``."""

    product_id: str
    title: str
    slug: str
    sku: str | None
    wc_status: str
    product_type: str
    regular_price: float
    price: float
    sale_price: float | None
    on_sale: bool
    manage_stock: bool
    stock_quantity: int | None
    stock_status: str
    categories: tuple[str, ...]
    category_ids: tuple[int, ...]
    description: str | None
    short_description: str | None
    image_url: str | None
    image_count: int
    options: dict[str, tuple[str, ...]]
    unit_cost: float | None
    updated_at: str | None
    variants: tuple[VariantRecord, ...] = ()

    @classmethod
    def from_node(
        cls, raw: dict[str, Any], variants: tuple[VariantRecord, ...] = ()
    ) -> ProductRecord:
        node = _Node(raw)
        regular, current = node.prices()
        images = node.rows("images")
        terms = node.rows("categories")
        return cls(
            product_id=node.text("id"),
            title=node.text("name"),
            slug=node.text("slug"),
            sku=node.optional_text("sku"),
            wc_status=node.text("status", "publish"),
            product_type=node.text("type", "simple"),
            regular_price=regular,
            price=current,
            sale_price=node.number("sale_price"),
            on_sale=node.flag("on_sale"),
            manage_stock=node.flag("manage_stock"),
            stock_quantity=node.count("stock_quantity"),
            stock_status=node.text("stock_status", "instock"),
            categories=tuple(str(term["name"]) for term in terms if term.get("name")),
            category_ids=tuple(
                term_id for term in terms if (term_id := _int_or_none(term.get("id"))) is not None
            ),
            description=strip_html(raw.get("description")),
            short_description=strip_html(raw.get("short_description")),
            image_url=images[0].get("src") if images else None,
            image_count=len(images),
            options=_variation_options(node.rows("attributes")),
            unit_cost=unit_cost_of(raw),
            updated_at=node.modified(),
            variants=variants,
        )

    # -- Derived facts ---------------------------------------------------------------

    @property
    def option_names(self) -> tuple[str, ...]:
        return tuple(self.options)

    @property
    def is_family(self) -> bool:
        return self.product_type == "variable" and len(self.variants) > 0

    @property
    def _level(self) -> _StockLevel:
        return _StockLevel(self.manage_stock, self.stock_quantity, self.stock_status)

    @property
    def price_range(self) -> tuple[float, float]:
        prices = [variant.regular_price for variant in self.variants] or [self.regular_price]
        return min(prices), max(prices)

    @property
    def listing_price(self) -> float:
        """Regular price shown in the catalog. For a variable product this is the lowest
        variation price regardless of stock, so the displayed figure does not jump when one
        size sells out."""
        return self.price_range[0] if self.is_family else self.regular_price

    @property
    def stock(self) -> int:
        if self.is_family:
            return sum(variant.stock for variant in self.variants)
        return self._level.units

    @property
    def tracks_inventory(self) -> bool:
        if self.is_family:
            return any(variant.manage_stock for variant in self.variants)
        return self.manage_stock

    @property
    def in_stock(self) -> bool:
        if self.is_family:
            return any(variant.in_stock for variant in self.variants)
        return self._level.available

    @property
    def category(self) -> str | None:
        """First real category name; WordPress's default ``Uncategorized`` term does not
        count."""
        for name in self.categories:
            if name.lower() != "uncategorized":
                return name
        return None

    @property
    def status(self) -> str:
        """Interface status for the product. Trashed products never get this far; the cache
        skips them on load."""
        if self.wc_status in HIDDEN_STATUSES:
            return "paused"
        if self.tracks_inventory:
            return "active" if self.in_stock else "out_of_stock"
        return "out_of_stock" if self.stock_status == "outofstock" else "active"

    @property
    def missing_content(self) -> tuple[str, ...]:
        checks = (
            ("long_description", len(self.description or "") < THIN_DESCRIPTION_CHARS),
            ("short_description", not self.short_description),
            ("images", self.image_count == 0),
            ("category", self.category is None),
        )
        return tuple(name for name, missing in checks if missing)

    @property
    def content_quality(self) -> str:
        gaps = len(self.missing_content)
        if gaps == 0:
            return "good"
        return "needs_work" if gaps == 1 else "poor"

    @property
    def summary(self) -> str | None:
        text = self.short_description or self.description
        return _clip(text, SUMMARY_CHARS) if text else None

    def variant(self, variant_id: str) -> VariantRecord | None:
        for candidate in self.variants:
            if candidate.variant_id == variant_id:
                return candidate
        return None

    def all_skus(self) -> list[str]:
        return [sku for sku in (self.sku, *(v.sku for v in self.variants)) if sku]

    # -- Interface models ------------------------------------------------------------

    def attributes(self) -> dict[str, str]:
        """Extra key/value facts included in the listing the model reads."""
        facts = {"slug": self.slug, "type": self.product_type}
        if self.sku:
            facts["sku"] = self.sku
        if self.is_family:
            facts["variants"] = str(len(self.variants))
            low, high = self.price_range
            if low != high:
                facts["price_range"] = f"{low:g}–{high:g}"
        elif self.on_sale and self.sale_price is not None:
            facts["sale_price"] = f"{self.sale_price:g}"
        return facts

    def _options_for_listing(self) -> dict[str, list[str]]:
        if not self.is_family:
            return {}
        return {name: list(values) for name, values in self.options.items()}

    def variant_listing(self, variant: VariantRecord, currency: str) -> Listing:
        if self.status == "paused" or variant.status != "publish":
            status = "paused"
        elif variant.in_stock or not variant.manage_stock:
            status = "active"
        else:
            status = "out_of_stock"
        facts: dict[str, str] = {}
        if variant.sku:
            facts["sku"] = variant.sku
        if variant.on_sale and variant.sale_price is not None:
            facts["sale_price"] = f"{variant.sale_price:g}"
        title = self.title
        if variant.title_suffix:
            title = f"{self.title} — {variant.title_suffix}"
        return Listing(
            listing_id=variant.variant_id,
            title=title,
            status=status,
            price=variant.regular_price,
            currency=currency,
            stock=variant.stock,
            category=self.category,
            attributes=facts,
            image_url=variant.image_url or self.image_url,
            option_values=variant.option_values,
            variant_of=self.product_id,
        )

    def to_listing(self, currency: str) -> Listing:
        return Listing(
            listing_id=self.product_id,
            title=self.title,
            status=self.status,
            price=self.listing_price,
            currency=currency,
            stock=self.stock,
            category=self.category,
            content_quality=self.content_quality,
            attributes=self.attributes(),
            image_url=self.image_url,
            short_description=self.summary,
            options=self._options_for_listing(),
        )

    def to_details(
        self,
        currency: str,
        *,
        sales_last_30d: int | None,
        return_rate_pct: float | None,
        review_snippets: list[str] | None = None,
    ) -> ListingDetails:
        return ListingDetails(
            **self.to_listing(currency).model_dump(),
            long_description=self.description,
            review_snippets=list(review_snippets or []),
            sales_last_30d=sales_last_30d,
            return_rate_pct=return_rate_pct,
            missing_attributes=list(self.missing_content),
            variants=[self.variant_listing(variant, currency) for variant in self.variants],
        )


@dataclass(frozen=True)
class ListingTarget:
    """A product, or one variation of it, addressed by a listing id. The staging and
    write paths both need "the thing whose price this is", and for a variation that means
    reading the variation's fields but naming the parent's title and REST path."""

    record: ProductRecord
    variant: VariantRecord | None = None

    @property
    def is_variation(self) -> bool:
        return self.variant is not None

    @property
    def listing_id(self) -> str:
        return self.variant.variant_id if self.variant else self.record.product_id

    @property
    def title(self) -> str:
        if self.variant and self.variant.title_suffix:
            return f"{self.record.title} — {self.variant.title_suffix}"
        return self.record.title

    @property
    def priced(self) -> ProductRecord | VariantRecord:
        """The object carrying the price and stock fields for this target."""
        return self.variant or self.record

    @property
    def needs_split(self) -> bool:
        """True when a variable parent is addressed directly. Prices and stock live on the
        variations, so the caller has to name each one."""
        return self.variant is None and self.record.is_family

    @property
    def variation_ids(self) -> list[str]:
        return [variant.variant_id for variant in self.record.variants]

    @property
    def rest_path(self) -> str:
        if self.variant is not None:
            return Routes.variation(self.record.product_id, self.variant.variant_id)
        return Routes.product(self.record.product_id)


# -- Search --------------------------------------------------------------------------------


def significant_terms(query: str) -> list[str]:
    tokens = (token.lower() for token in _TOKEN.findall(query))
    return [token for token in tokens if len(token) > 1 and token not in _NOISE_WORDS]


def is_browse(query: str) -> bool:
    """True for queries like "show me everything" that name the whole catalog rather than
    a product in it."""
    return _WHOLE_CATALOG_WORDS.issuperset(significant_terms(query))


def _search_fields(record: ProductRecord) -> tuple[tuple[str, int], ...]:
    """Lower-cased text fields paired with their weight, in matching order."""
    return (
        (record.title.lower(), _TITLE_WEIGHT),
        (" ".join(record.categories).lower(), _CATEGORY_WEIGHT),
        (record.slug.replace("-", " ").lower(), _TEXT_WEIGHT),
        (" ".join(record.all_skus()).lower(), _SKU_WEIGHT),
        ((record.description or "").lower(), _TEXT_WEIGHT),
    )


def score(record: ProductRecord, terms: list[str]) -> int:
    """Weighted term match over title, categories, slug, SKUs, and description. Ranks the
    site's search results, and doubles as the local scorer when the site returns none."""
    fields = _search_fields(record)
    return sum(next((weight for text, weight in fields if term in text), 0) for term in terms)


def _rank(records: Iterable[ProductRecord], terms: list[str], limit: int) -> list[ProductRecord]:
    scored = [(score(record, terms), record) for record in records]
    scored.sort(key=lambda pair: (-pair[0], pair[1].title.lower()))
    return [record for points, record in scored if points > 0][:limit]


def _live_nodes(nodes: Iterable[Any]) -> Iterator[dict[str, Any]]:
    """Dict rows that are not in the trash."""
    for node in nodes:
        if isinstance(node, dict) and node.get("status") != "trash":
            yield node


# -- Cache ---------------------------------------------------------------------------------


class CatalogCache:
    """Paged read of ``wc/v3/products`` with ``status=any``, kept for ``ttl_s`` seconds.
    One copy serves the portal's listing pages, the inventory rules, and the search
    fallback; a successful write triggers a refresh. Variable products have their
    variations fetched alongside, so a record is complete by the time it is cached."""

    def __init__(
        self,
        executor: WooExecutor,
        *,
        page_size: int = 100,
        max_products: int = 250,
        ttl_s: float = 45.0,
    ) -> None:
        self._executor = executor
        self._page_size = page_size
        self._max_products = max_products
        self._ttl_s = ttl_s
        self._by_id: dict[str, ProductRecord] = {}
        self._parent_by_variant: dict[str, str] = {}
        # Monotonic deadline after which the next read reloads; zero means never loaded.
        self._fresh_until = 0.0

    # -- Loading -----------------------------------------------------------------------

    def invalidate(self) -> None:
        self._fresh_until = 0.0

    def cached(self) -> list[ProductRecord]:
        return sorted(self._by_id.values(), key=lambda record: record.title.lower())

    async def all(self) -> list[ProductRecord]:
        if time.monotonic() > self._fresh_until:
            await self._reload()
        return self.cached()

    async def refresh(self) -> list[ProductRecord]:
        """Reload immediately instead of merely invalidating. ``cached()`` is used by the
        portal's synchronous handlers, which cannot trigger a fetch themselves."""
        self.invalidate()
        return await self.all()

    async def _reload(self) -> None:
        nodes = await paged(
            self._executor,
            Routes.products,
            {"status": "any", "orderby": "title", "order": "asc"},
            per_page=self._page_size,
            max_items=self._max_products,
        )
        records = [await self._build(node) for node in _live_nodes(nodes)]
        self._by_id = {record.product_id: record for record in records}
        self._parent_by_variant = {
            variant.variant_id: record.product_id
            for record in records
            for variant in record.variants
        }
        self._fresh_until = time.monotonic() + self._ttl_s

    async def _build(self, node: dict[str, Any]) -> ProductRecord:
        """Record for one product node, fetching the variation rows of a variable
        product."""
        product_id = str(node["id"])
        variants: tuple[VariantRecord, ...] = ()
        if node.get("type") == "variable":
            rows = await paged(
                self._executor,
                Routes.variations(product_id),
                {"status": "any"},
                per_page=100,
                max_items=100,
            )
            variants = tuple(VariantRecord.from_node(row, product_id) for row in _live_nodes(rows))
        return ProductRecord.from_node(node, variants)

    def _remember(self, record: ProductRecord) -> ProductRecord:
        self._by_id[record.product_id] = record
        for variant in record.variants:
            self._parent_by_variant[variant.variant_id] = record.product_id
        return record

    # -- Lookups -----------------------------------------------------------------------

    def parent_of(self, listing_id: str) -> str | None:
        """Parent product id for a cached variation id."""
        return self._parent_by_variant.get(listing_id)

    async def _fetch(self, post_id: str) -> dict[str, Any] | None:
        """One ``GET wc/v3/products/{id}``; None when the site has no such post."""
        try:
            body, _ = await self._executor.request("GET", Routes.product(post_id))
        except WooApiError as error:
            if error.status == 404 or "invalid_id" in error.code:
                return None
            raise
        return body if isinstance(body, dict) else None

    async def get(self, listing_id: str) -> ProductRecord | None:
        """Fetch one product by id, slug, title, or SKU, bypassing the TTL so a staged
        change is compared against current data. A variation id is redirected to its parent
        (``resolve`` then picks out the variation). Non-numeric input is matched against
        cached slugs, titles, and SKUs first."""
        wanted: str | None = listing_id.strip()
        if wanted and not wanted.isdigit():
            wanted = await self._id_for_name(wanted)
        if not wanted:
            return None
        node = await self._fetch(wanted)
        if node is None:
            return None
        if node.get("type") == "variation" and node.get("parent_id"):
            return await self.get(str(node["parent_id"]))
        if node.get("status") == "trash":
            return None
        return self._remember(await self._build(node))

    async def resolve(self, listing_id: str) -> tuple[ProductRecord, VariantRecord | None] | None:
        """``(product, None)`` for a product id, ``(parent, variation)`` for a variation id,
        None when neither exists. Both paths go through a fresh ``get``."""
        target = await self.target(listing_id)
        return (target.record, target.variant) if target else None

    async def target(self, listing_id: str) -> ListingTarget | None:
        """``resolve`` packaged as a :class:`ListingTarget`."""
        wanted = listing_id.strip()
        record = await self.get(wanted)
        if record is None:
            return None
        if record.product_id == wanted:
            return ListingTarget(record)
        variant = record.variant(wanted)
        return ListingTarget(record, variant) if variant else None

    async def _id_for_name(self, text: str) -> str | None:
        """Product id whose slug, title, or any SKU equals ``text`` (case-insensitive)."""
        wanted = text.casefold()
        for record in await self.all():
            names = (record.slug, record.title, *record.all_skus())
            if any(name.casefold() == wanted for name in names):
                return record.product_id
        return None

    # -- Search --------------------------------------------------------------------------

    async def _site_search(self, terms: list[str], limit: int) -> list[ProductRecord]:
        """Ask WooCommerce for matches (it searches title, content, and SKU) and cache
        each one. An API refusal yields an empty list rather than an error."""
        page = max(limit * 3, 20)
        try:
            nodes = await paged(
                self._executor,
                Routes.products,
                {"search": " ".join(terms[:6]), "status": "any"},
                per_page=page,
                max_items=page,
            )
        except WooApiError as error:
            logger.info("wc/v3/products search failed, scoring the cached catalog: %s", error)
            return []
        return [self._remember(await self._build(node)) for node in _live_nodes(nodes)]

    async def search(self, query: str, limit: int) -> list[ProductRecord]:
        """Search through the site's ``search`` parameter, then re-rank locally with
        ``score``. If the site returns nothing, the cached catalog is scored with the same
        terms, so a longer descriptive phrase still finds its product."""
        if is_browse(query):
            return (await self.all())[:limit]
        # An exact SKU, slug, or title is a direct lookup. Text scoring would tie the exact
        # match with every product sharing the prefix.
        exact = await self._id_for_name(query.strip())
        if exact is not None:
            record = await self.get(exact)
            return [record] if record else []
        terms = significant_terms(query)
        candidates = await self._site_search(terms, limit) or await self.all()
        return _rank(candidates, terms, limit)
