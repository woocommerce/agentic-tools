# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""Applying approved changes to WooCommerce. This is the sole module that sends PUT
requests.

Every tool the agent can call ends at the change ledger; no tool path reaches this file.
``WooWriter.apply`` runs only after the host has recorded an operator's approval, and it
converts the ledger items into ``wc/v3`` writes.

Sequence: write, then advance the ledger. The backend re-runs the guardrails before calling
in, so a config made stricter after staging still stops the write. Each write also compares
the value it is replacing against what the site holds now, and refuses when the two differ,
so an edit made on the store between approval and apply is not silently overwritten. The ledger entry moves to
applied only once WooCommerce has returned success. A change the site refused outright keeps
its staged status and can be approved again; one that wrote some targets before failing, or
whose failure leaves the outcome unknown, is discarded instead, because its ``before`` values
no longer describe the store and replaying it would only be refused.

Change kinds handled: price updates set ``regular_price`` on the product or variation;
listing updates set the content fields in ``LISTING_FIELDS``; inventory actions set
``stock_quantity`` (after re-reading the live count) or switch ``status`` between
``publish`` and ``draft`` (``private`` for a variation); promotions set ``sale_price``
together with ``date_on_sale_from`` and ``date_on_sale_to``. Campaigns are not handled:
WooCommerce core has no campaign object, and the campaign tools are disabled in the
config.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from merchant_agent import ChangeItem, ChangeKind, StagedChange
from merchant_agent.changes import ChangeNotApplicable

from .catalog import CatalogCache, ListingTarget
from .rest_client import Routes, WooApiError, WooExecutor

PRICE_FIELD = "price"
STOCK_FIELD = "stock"
STATUS_FIELD = "status"
SALE_PRICE_FIELD = "sale_price"

# Interface field name -> wc/v3 product field. ``description`` and ``long_description``
# both land on WooCommerce's ``description``, so a change carrying both with different
# values is rejected. ``category`` is a name that gets looked up in
# ``wc/v3/products/categories`` when the change is applied.
LISTING_FIELDS: dict[str, str] = {
    "title": "name",
    "description": "description",
    "long_description": "description",
    "short_description": "short_description",
    "category": "categories",
    "sku": "sku",
    "slug": "slug",
}

# Interface status -> WordPress post status, for a product and for a variation. A hidden
# variation is ``private`` because WooCommerce does not let variations be drafts.
_POST_STATUS = {
    "active": ("publish", "publish"),
    "paused": ("draft", "private"),
}

# Interface field -> the live value to compare a staged ``before`` against. ``category`` is
# absent because its live form is a tuple of names and a staged change carries one.
_LIVE_LISTING: dict[str, Callable[[Any], object]] = {
    "title": lambda record: record.title,
    "description": lambda record: record.description,
    "long_description": lambda record: record.description,
    "short_description": lambda record: record.short_description,
    "sku": lambda record: record.sku,
    "slug": lambda record: record.slug,
}

Notes = list[str]
_Handler = Callable[[ListingTarget, list[ChangeItem], dict[str, Any]], Awaitable[Notes]]


class WriteFailed(RuntimeError):
    """WooCommerce rejected a write. ``completed`` lists the targets already written as part
    of the same change, so the operator learns the apply was partial rather than a no-op, and
    ``uncertain`` marks a failure that may still have changed the store: a 5xx says nothing
    about what WooCommerce did, where a refusal certainly wrote nothing. Both are filled in
    by :meth:`WooWriter.apply`, which is the only place that knows what ran before the
    failure, so the text is composed on read rather than at construction."""

    def __init__(
        self, message: str, completed: list[str] | None = None, *, uncertain: bool = False
    ) -> None:
        super().__init__(message)
        self.reason = message
        self.completed = list(completed or [])
        self.uncertain = uncertain

    def __str__(self) -> str:
        text = self.reason
        if self.uncertain:
            text += ". That write may still have reached the store"
        elif self.completed:
            text += ". That write did not reach the store"
        if self.completed:
            text += ". Already updated: " + ", ".join(self.completed)
        return text


def _may_have_landed(error: WooApiError) -> bool:
    """Whether a rejected write might have been applied anyway. A 5xx, or a failure with no
    status at all, says nothing about what the store did; a 4xx refused it outright."""
    return error.status >= 500 or error.status == 0


def _money(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _units(value: object) -> int:
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def _same(staged: object, live: object) -> bool:
    if isinstance(staged, (int, float)) and isinstance(live, (int, float)):
        return round(float(staged), 2) == round(float(live), 2)
    return str(staged or "").strip() == str(live or "").strip()


def _refuse_if_moved(target: ListingTarget, label: str, staged: object, live: object) -> None:
    """Refuse a write whose starting point is no longer what the store holds.

    The operator approved a move away from ``before``, so a value that has changed since
    staging can invert the decision: an approved rise from 19.99 to 24.99 becomes a cut once
    the shop has already moved to 30.00. Refusing keeps the change staged for a fresh look.

    Status is deliberately not checked this way. Pausing or activating states an absolute
    intent that a stale starting point cannot invert, so re-reading it would only produce
    refusals that the operator has no reason to act on.

    Every kind that does get here records a ``before``, so an absent one means the field was
    empty rather than unknown, and a field filled in since staging is a change to refuse."""
    if _same(staged, live):
        return
    raise WriteFailed(
        f"{target.title}: WooCommerce now holds {label} {live}, not {staged}; it moved since "
        "the change was staged. Stage the change again from the current value"
    )


def _only(items: list[ChangeItem], field: str) -> ChangeItem | None:
    return next((item for item in items if item.field == field), None)


def _grouped_by_target(items: list[ChangeItem]) -> list[tuple[str, list[ChangeItem]]]:
    """Items per target id, in order of first appearance, so each product or variation
    receives one PUT per change."""
    order = list(dict.fromkeys(item.target for item in items))
    return [(target, [item for item in items if item.target == target]) for target in order]


class WooWriter:
    """Turns an approved ``StagedChange`` into REST writes."""

    def __init__(self, executor: WooExecutor, catalog: CatalogCache) -> None:
        self.executor = executor
        self.catalog = catalog
        self._handlers: dict[ChangeKind, _Handler] = {
            ChangeKind.PRICE_UPDATE: self._regular_price,
            ChangeKind.LISTING_UPDATE: self._content,
            ChangeKind.INVENTORY_ACTION: self._inventory,
            ChangeKind.PROMOTION: self._sale_window,
        }

    async def apply(
        self, change: StagedChange, *, promotion_window: dict[str, Any] | None = None
    ) -> Notes:
        """Apply ``change`` and return notes for the operator. Raises :class:`WriteFailed`
        when the site refuses a write (that target is left untouched) or
        :class:`ChangeNotApplicable` when the change targets something this deployment does
        not manage. ``promotion_window`` holds a promotion's ``starts`` and ``ends``, which
        the backend stores beside the change."""
        if change.kind is ChangeKind.CAMPAIGN:
            raise ChangeNotApplicable(
                "campaign changes cannot be applied: WooCommerce core has no campaign object"
            )
        handler = self._handlers.get(change.kind)
        if handler is None:
            raise ChangeNotApplicable(
                f"{change.kind.value} changes have no writer in this deployment"
            )
        # Targets are written sequentially over ordinary wc/v3 calls, with no transaction
        # and no compensating rollback: a later failure leaves the earlier writes applied,
        # and WriteFailed carries the list of what landed. A production host picks the
        # transaction and rollback semantics its store needs.
        notes: Notes = []
        done: list[str] = []
        for listing_id, items in _grouped_by_target(change.items):
            target = await self.catalog.target(listing_id)
            if target is None:
                raise WriteFailed(
                    f"product {listing_id} could not be found in the catalog; nothing was "
                    "written for it",
                    done,
                )
            try:
                notes += await handler(target, items, promotion_window or {})
            except WooApiError as error:
                raise WriteFailed(str(error), done, uncertain=_may_have_landed(error)) from error
            except WriteFailed as error:
                # A handler refusing a write knows why, but only this loop knows what it
                # already wrote for the same change.
                if not error.completed:
                    error.completed = list(done)
                raise
            done.append(target.title)
        # Refresh now rather than mark stale: the portal's next render reads the cache
        # synchronously and should already show the new values.
        await self.catalog.refresh()
        return notes

    async def _put(self, target: ListingTarget, body: dict[str, Any]) -> Any:
        payload, _ = await self.executor.request("PUT", target.rest_path, json=body)
        return payload

    # -- regular_price -------------------------------------------------------------------

    async def _regular_price(
        self, target: ListingTarget, items: list[ChangeItem], _: dict[str, Any]
    ) -> Notes:
        item = _only(items, PRICE_FIELD)
        new_price = _money(item.after) if item else None
        if item is None or new_price is None:
            raise WriteFailed(f"price item for {target.record.title} has no numeric target price")
        if target.needs_split:
            raise ChangeNotApplicable(
                f"{target.record.title} has {len(target.record.variants)} priced variations; "
                "price each variation by its own id"
            )
        _refuse_if_moved(target, "a regular price of", item.before, target.priced.regular_price)
        await self._put(target, {"regular_price": f"{new_price:.2f}"})
        priced = target.priced
        if priced.on_sale and priced.sale_price is not None:
            return [
                f"{target.title} has an active sale price of {priced.sale_price:.2f}; only "
                "regular_price changed, and customers pay the sale price until it expires"
            ]
        return []

    # -- Content fields ------------------------------------------------------------------

    async def _content(
        self, target: ListingTarget, items: list[ChangeItem], _: dict[str, Any]
    ) -> Notes:
        parent = target.record.product_id
        if target.is_variation:
            raise ChangeNotApplicable(
                f"content fields live on the parent product {parent}; stage the edit against {parent}"
            )
        body: dict[str, Any] = {}
        sources: dict[str, ChangeItem] = {}
        for item in items:
            wc_field = LISTING_FIELDS.get(item.field)
            if wc_field is None:
                raise ChangeNotApplicable(
                    f"listing field '{item.field}' is not one this deployment writes"
                )
            earlier = sources.get(wc_field)
            if earlier is not None and earlier.after != item.after:
                raise ChangeNotApplicable(
                    f"both '{earlier.field}' and '{item.field}' map to the WooCommerce field "
                    f"{wc_field}; stage one of them with one value"
                )
            sources[wc_field] = item
            live = _LIVE_LISTING.get(item.field)
            if live is not None:
                _refuse_if_moved(target, f"a {item.field} of", item.before, live(target.record))
            if wc_field == "categories":
                body[wc_field] = [{"id": await self._category_id(str(item.after))}]
            else:
                body[wc_field] = item.after
        await self._put(target, body)
        return []

    async def _category_id(self, name: str) -> int:
        """Term id for a category name (or slug), via ``wc/v3/products/categories``."""
        found, _ = await self.executor.request(
            "GET", Routes.categories, params={"search": name, "per_page": 20}
        )
        terms = [
            term for term in (found if isinstance(found, list) else []) if isinstance(term, dict)
        ]
        wanted = name.strip().casefold()
        for key in ("name", "slug"):
            for term in terms:
                if str(term.get(key, "")).casefold() == wanted:
                    return int(term["id"])
        raise WriteFailed(f"no product category on this store matches {name!r}")

    # -- stock_quantity and status -------------------------------------------------------

    async def _inventory(
        self, target: ListingTarget, items: list[ChangeItem], _: dict[str, Any]
    ) -> Notes:
        notes: Notes = []
        for item in items:
            if item.field == STATUS_FIELD:
                notes += await self._post_status(target, item)
            elif item.field == STOCK_FIELD:
                notes += await self._stock_quantity(target, item)
            else:
                raise ChangeNotApplicable(
                    f"inventory field '{item.field}' has no writer in this deployment"
                )
        return notes

    async def _post_status(self, target: ListingTarget, item: ChangeItem) -> Notes:
        wanted = str(item.after).lower()
        if wanted not in _POST_STATUS:
            raise ChangeNotApplicable(
                f"status '{item.after}' is not one this deployment writes (active or paused)"
            )
        for_product, for_variation = _POST_STATUS[wanted]
        status = for_variation if target.is_variation else for_product
        await self._put(target, {"status": status})
        if status == "draft":
            return [
                f"{target.record.title} is now in draft status; WooCommerce removes drafts from "
                "the shop, feeds, and the Store API, not just from search results"
            ]
        return []

    async def _stock_quantity(self, target: ListingTarget, item: ChangeItem) -> Notes:
        staged_before, staged_after = _units(item.before), _units(item.after)
        if staged_after == staged_before:
            return []
        if target.needs_split:
            raise ChangeNotApplicable(
                f"{target.record.title} tracks stock on each variation; restock each variation "
                "by its own id"
            )
        priced = target.priced
        if not priced.manage_stock:
            raise ChangeNotApplicable(
                f"{target.title} does not track inventory in WooCommerce, so there is no "
                "stock_quantity to change"
            )
        # A restock is a delta, and a delta laid on top of a stale count is exactly how
        # stock drifts, so the starting figure is checked like every other field's.
        live = priced.stock_quantity or 0
        _refuse_if_moved(target, "a stock quantity of", staged_before, live)
        new_count = max(0, live + (staged_after - staged_before))
        await self._put(target, {"manage_stock": True, "stock_quantity": new_count})
        return []

    # -- sale_price with a window ----------------------------------------------------------

    async def _sale_window(
        self, target: ListingTarget, items: list[ChangeItem], window: dict[str, Any]
    ) -> Notes:
        item = _only(items, SALE_PRICE_FIELD)
        sale_price = _money(item.after) if item else None
        if item is None or sale_price is None:
            raise WriteFailed(f"promotion item for {target.record.title} has no numeric sale price")
        if target.needs_split:
            raise ChangeNotApplicable(
                f"{target.record.title} is a variable product; the promotion must list its "
                "variations individually"
            )
        # ``before`` is the full price the discount was computed from, not a sale price.
        _refuse_if_moved(target, "a regular price of", item.before, target.priced.regular_price)
        starts, ends = window.get("starts"), window.get("ends")
        body: dict[str, Any] = {"sale_price": f"{sale_price:.2f}"}
        if starts:
            body["date_on_sale_from"] = f"{starts}T00:00:00"
        if ends:
            body["date_on_sale_to"] = f"{ends}T23:59:59"
        await self._put(target, body)
        span = "".join((f" from {starts}" if starts else "", f" to {ends}" if ends else ""))
        return [
            f"{target.title} now has a scheduled sale price of {sale_price:.2f}{span}; "
            "WooCommerce activates and clears it on those dates"
        ]
