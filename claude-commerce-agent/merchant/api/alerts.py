# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""Inventory alerts and order issues, derived on demand from the catalog and order caches.

WooCommerce can email a low-stock notice but keeps no alert list to query, so every alert
here is the result of a rule evaluated against current data. The thresholds are read from
``data/thresholds.json`` (with environment overrides for two of them), which keeps the
numbers the agent cites in one editable place.

Of the four ``OrderIssue`` kinds in the interface, three can be derived. ``delayed``: a
paid order still ``processing`` after the fulfilment SLA. ``return_spike``: a product whose
refunded share of units crosses a threshold. ``buyer_message``: the ``customer_note`` field
on an open order, which WooCommerce stores directly on the order record, so no extra API
permission is involved. ``damaged`` has no data source in WooCommerce and is never
produced.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from merchant_agent import AlertCounts, InventoryAlert, OrderIssue

from .catalog import CatalogCache, ProductRecord, VariantRecord
from .orders import OrderRecord, OrderScan, refund_units_by_product, units_by_product

NOTE_EXCERPT_CHARS = 160
SALES_WINDOW_DAYS = 30

InventoryKind = Literal["low_stock", "slow_mover"]


def _whole(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class _ThresholdFile:
    """Tolerant reads of ``thresholds.json``: a missing file, a non-object document, or a
    malformed value all fall back to the built-in default for that key."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    @classmethod
    def read(cls, path: Path) -> _ThresholdFile:
        document: Any = json.loads(path.read_text()) if path.exists() else {}
        return cls(document if isinstance(document, dict) else {})

    def decimal(self, key: str, default: float) -> float:
        try:
            return float(self._values[key])
        except (KeyError, TypeError, ValueError):
            return default

    def whole(self, key: str, default: int) -> int:
        return int(self.decimal(key, default))

    def mapping(self, key: str) -> dict[str, int]:
        raw = self._values.get(key)
        if not isinstance(raw, dict):
            return {}
        return {
            str(name): limit for name, value in raw.items() if (limit := _whole(value)) is not None
        }


@dataclass(frozen=True)
class AlertRules:
    """Threshold values for the alert rules."""

    low_stock_default: int = 8
    low_stock_by_category: dict[str, int] = field(default_factory=dict)
    low_stock_by_slug: dict[str, int] = field(default_factory=dict)
    slow_mover_max_units_30d: int = 2
    slow_mover_min_stock: int = 12
    fulfilment_sla_days: int = 3
    return_spike_pct: float = 20.0
    return_spike_min_units: int = 4

    @classmethod
    def load(cls, path: Path, *, low_stock_default: int, fulfilment_sla_days: int) -> AlertRules:
        """Rules from ``path`` with the two deployment settings layered over it."""
        file = _ThresholdFile.read(path)
        return cls(
            low_stock_default=low_stock_default,
            low_stock_by_category=file.mapping("low_stock_by_category"),
            low_stock_by_slug=file.mapping("low_stock_by_slug"),
            slow_mover_max_units_30d=file.whole("slow_mover_max_units_30d", 2),
            slow_mover_min_stock=file.whole("slow_mover_min_stock", 12),
            fulfilment_sla_days=fulfilment_sla_days,
            return_spike_pct=file.decimal("return_spike_pct", 20.0),
            return_spike_min_units=file.whole("return_spike_min_units", 4),
        )

    def low_stock_threshold(self, record: ProductRecord) -> int:
        """Most specific rule wins: a per-slug threshold, then a per-category one, falling
        back to the deployment-wide default."""
        candidates = (
            self.low_stock_by_slug.get(record.slug),
            *(self.low_stock_by_category.get(name) for name in record.categories),
        )
        return next((limit for limit in candidates if limit is not None), self.low_stock_default)


# -- Inventory -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _StockSubject:
    """The unit the inventory rules judge: a simple product, or one variation of a variable
    product. WooCommerce holds ``stock_quantity`` per variation, so the alert names the
    variation that is running low rather than its parent."""

    listing_id: str
    title: str
    stock: int
    tracked: bool
    visible: bool
    threshold: int
    sold: int
    option_values: dict[str, str] = field(default_factory=dict)
    variant_of: str | None = None

    @classmethod
    def product(cls, record: ProductRecord, threshold: int, units: dict[str, int]) -> _StockSubject:
        return cls(
            listing_id=record.product_id,
            title=record.title,
            stock=record.stock,
            tracked=record.tracks_inventory,
            visible=record.status == "active",
            threshold=threshold,
            sold=units.get(record.product_id, 0),
        )

    @classmethod
    def variation(
        cls, record: ProductRecord, variant: VariantRecord, threshold: int, units: dict[str, int]
    ) -> _StockSubject:
        return cls(
            listing_id=variant.variant_id,
            title=f"{record.title} — {variant.title_suffix}",
            stock=variant.stock,
            tracked=variant.manage_stock,
            visible=variant.in_stock,
            threshold=threshold,
            sold=units.get(variant.variant_id, 0),
            option_values=variant.option_values,
            variant_of=record.product_id,
        )

    @property
    def days_of_cover(self) -> float | None:
        if not self.sold:
            return None
        daily_pace = self.sold / SALES_WINDOW_DAYS
        return round(self.stock / daily_pace, 1)


def _stock_subjects(
    records: list[ProductRecord], units: dict[str, int], rules: AlertRules
) -> Iterator[_StockSubject]:
    """Every product or variation the inventory rules should look at. Paused products are
    off the shop already and are left out."""
    for record in records:
        if record.status == "paused":
            continue
        threshold = rules.low_stock_threshold(record)
        if record.is_family:
            for variant in record.variants:
                yield _StockSubject.variation(record, variant, threshold, units)
        else:
            yield _StockSubject.product(record, threshold, units)


def _alert_order(alert: InventoryAlert) -> tuple[int, int]:
    """Low-stock alerts first, then the shallowest stock. Both the portal list and the tool
    output use this ordering."""
    return (0 if alert.kind == "low_stock" else 1, alert.stock)


class IssueRules:
    """The alert rules as methods over already-fetched data. ``AlertBuilder`` runs them
    twice over: on fresh caches for the agent's tools and on whatever is cached for the
    portal's synchronous routes."""

    def __init__(self, rules: AlertRules) -> None:
        self.rules = rules

    # -- Stock -----------------------------------------------------------------------

    def classify(self, subject: _StockSubject) -> InventoryKind | None:
        if subject.tracked and subject.stock <= subject.threshold:
            return "low_stock"
        slow = subject.sold <= self.rules.slow_mover_max_units_30d
        if slow and subject.stock >= self.rules.slow_mover_min_stock:
            return "slow_mover"
        return None

    def inventory(
        self, records: list[ProductRecord], units: dict[str, int]
    ) -> list[InventoryAlert]:
        alerts: list[InventoryAlert] = []
        for subject in _stock_subjects(records, units, self.rules):
            kind = self.classify(subject)
            if kind is None:
                continue
            alerts.append(
                InventoryAlert(
                    listing_id=subject.listing_id,
                    title=subject.title,
                    kind=kind,
                    option_values=subject.option_values,
                    variant_of=subject.variant_of,
                    stock=subject.stock,
                    threshold=subject.threshold if kind == "low_stock" else None,
                    days_of_cover=subject.days_of_cover,
                    sales_last_30d=subject.sold,
                    storefront_visible=subject.visible,
                )
            )
        return sorted(alerts, key=_alert_order)

    # -- Orders ----------------------------------------------------------------------

    def delayed(
        self, orders: tuple[OrderRecord, ...], *, now: datetime | None = None
    ) -> list[OrderIssue]:
        """Paid orders still in ``processing`` for longer than ``fulfilment_sla_days``. The
        status comes from WooCommerce; the SLA is a deployment setting."""
        moment = now or datetime.now(UTC)
        sla = self.rules.fulfilment_sla_days
        deadline = moment - timedelta(days=sla)
        late = [order for order in orders if order.is_open and order.created_at <= deadline]
        late.sort(key=lambda order: order.created_at)
        return [
            OrderIssue(
                issue_id=f"delayed-{order.order_id}",
                order_id=order.number,
                kind="delayed",
                summary=(
                    f"{order.number} has sat in processing for {(moment - order.created_at).days} "
                    f"days ({order.units} units); the fulfilment window is {sla} days"
                ),
                listing_id=_first_product(order),
                opened_at=order.created_at,
            )
            for order in late
        ]

    def buyer_messages(self, orders: tuple[OrderRecord, ...]) -> list[OrderIssue]:
        """Open orders whose ``customer_note`` is non-empty. The excerpt is customer-written
        text; the tool executor fences it before the model reads it."""
        return [
            OrderIssue(
                issue_id=f"note-{order.order_id}",
                order_id=order.number,
                kind="buyer_message",
                summary=f"{order.number} includes a customer note that has not been addressed",
                listing_id=_first_product(order),
                buyer_message_excerpt=_excerpt(order.customer_note or ""),
                opened_at=order.created_at,
            )
            for order in orders
            if order.is_open and order.customer_note
        ]

    def return_spikes(
        self, sold: dict[str, int], refunded: dict[str, int], titles: dict[str, str]
    ) -> list[OrderIssue]:
        """Products whose refunded units exceed ``return_spike_pct`` of units sold, with at
        least ``return_spike_min_units`` refunded. Because a WooCommerce refund attaches to
        the order, every line on that order is counted; treat the result as a prompt to look
        at the product, not as a precise return rate."""
        issues: list[OrderIssue] = []
        for product_id in sorted(refunded):
            # Variation ids are counted too but skipped here; the parent gets flagged.
            if product_id not in titles:
                continue
            came_back, went_out = refunded[product_id], sold.get(product_id, 0)
            if went_out <= 0 or came_back < self.rules.return_spike_min_units:
                continue
            rate = round(came_back / went_out * 100, 1)
            if rate < self.rules.return_spike_pct:
                continue
            issues.append(
                OrderIssue(
                    issue_id=f"returns-{product_id}",
                    order_id=f"{came_back} refunded units ({SALES_WINDOW_DAYS}-day window)",
                    kind="return_spike",
                    summary=(
                        f"{titles[product_id]}: {rate}% of the {went_out} units sold over the "
                        f"past {SALES_WINDOW_DAYS} days came back as refunds"
                    ),
                    listing_id=product_id,
                )
            )
        return issues

    def order_issues(
        self, orders: tuple[OrderRecord, ...], records: list[ProductRecord]
    ) -> list[OrderIssue]:
        titles = {record.product_id: record.title for record in records}
        window = SALES_WINDOW_DAYS
        return [
            *self.delayed(orders),
            *self.buyer_messages(orders),
            *self.return_spikes(
                units_by_product(orders, window), refund_units_by_product(orders, window), titles
            ),
        ]


def _first_product(order: OrderRecord) -> str | None:
    return order.lines[0].product_id if order.lines else None


def _excerpt(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= NOTE_EXCERPT_CHARS:
        return flat
    return flat[: NOTE_EXCERPT_CHARS - 1] + "…"


def _tally(inventory: list[InventoryAlert], issues: list[OrderIssue], pending: int) -> AlertCounts:
    kinds = [alert.kind for alert in inventory]
    return AlertCounts(
        low_stock=kinds.count("low_stock"),
        slow_movers=kinds.count("slow_mover"),
        order_issues=len(issues),
        pending_changes=pending,
    )


class AlertBuilder:
    """Runs :class:`IssueRules` over ``CatalogCache`` and ``OrderScan``. The ``*_cached``
    methods skip fetching and exist for the portal's synchronous handlers."""

    def __init__(self, catalog: CatalogCache, orders: OrderScan, rules: AlertRules) -> None:
        self._catalog = catalog
        self._orders = orders
        self._rules = IssueRules(rules)

    @property
    def rules(self) -> AlertRules:
        return self._rules.rules

    async def inventory_alerts(self) -> list[InventoryAlert]:
        records = await self._catalog.all()
        units = await self._orders.units_by_product(days=SALES_WINDOW_DAYS)
        return self._rules.inventory(records, units)

    def inventory_alerts_cached(self) -> list[InventoryAlert]:
        units = units_by_product(self._orders.cached(), SALES_WINDOW_DAYS)
        return self._rules.inventory(self._catalog.cached(), units)

    async def order_issues(self) -> list[OrderIssue]:
        return self._rules.order_issues(await self._orders.orders(), await self._catalog.all())

    def order_issues_cached(self) -> list[OrderIssue]:
        return self._rules.order_issues(self._orders.cached(), self._catalog.cached())

    async def counts(self, pending_changes: int) -> AlertCounts:
        return _tally(await self.inventory_alerts(), await self.order_issues(), pending_changes)
