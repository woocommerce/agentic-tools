# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""Recent orders, fetched once from ``wc/v3/orders`` and reused by every consumer.

The same scan feeds four things: the day-by-day series used when WooCommerce Analytics is
unavailable, per-product unit counts for demand signals and slow-mover detection, the
open-order checks behind the order issues, and the portal's recent-orders panel. One paged
read with ``after``, ``status=any``, and ``orderby=date`` replaces a separate query for
each of them.

``window_days`` (60 by default) is a limit chosen here, not one WooCommerce imposes; the
REST route can page through a store's entire history. Sixty days covers the alert rules and
a period-over-period comparison of up to a month. Any reporting period gets clipped to this
window, and ``get_merchant_context`` tells the agent so.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .rest_client import Routes, WooExecutor, paged

# A product-id filter for the aggregations; None means every order counts.
Subset = frozenset[str] | None

# A paid order the merchant has yet to ship. ``pending`` and ``on-hold`` are waiting on the
# customer's payment instead, so they never count as late fulfilment.
AWAITING_FULFILMENT = "processing"
# Statuses that never count toward sales figures: unpaid, undone, or gone.
NOT_A_SALE = frozenset({"pending", "cancelled", "refunded", "failed", "trash"})
_GMT_FORMAT = "%Y-%m-%dT%H:%M:%S"


def parse_time(raw: str | None) -> datetime | None:
    """Parse a WooCommerce timestamp. ``*_gmt`` fields carry no offset and are UTC; other
    date fields may carry one."""
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _amount(value: Any) -> float:
    """A money string as a float, 0.0 when absent or malformed."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _first_present(node: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if node.get(key):
            return node[key]
    return None


@dataclass(frozen=True)
class OrderLine:
    title: str
    quantity: int
    # ``line_items[].total``: what the customer paid for the line after discounts.
    revenue: float
    product_id: str | None
    variant_id: str | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> OrderLine:
        product = row.get("product_id")
        variation = row.get("variation_id")
        return cls(
            title=str(row.get("name") or ""),
            quantity=int(row.get("quantity") or 0),
            revenue=_amount(row.get("total")),
            product_id=str(product) if product else None,
            variant_id=str(variation) if variation else None,
        )

    @property
    def keys(self) -> tuple[str, ...]:
        """Every id this line should be counted under: the product, and the variation
        when the line names one."""
        return tuple(key for key in (self.product_id, self.variant_id) if key)


@dataclass(frozen=True)
class OrderRecord:
    order_id: str
    number: str
    status: str
    created_at: datetime
    paid_at: datetime | None
    total: float
    currency: str
    refunded_amount: float
    customer_note: str | None
    lines: tuple[OrderLine, ...]

    @classmethod
    def from_node(cls, node: dict[str, Any]) -> OrderRecord | None:
        stamp = _first_present(node, "date_created_gmt", "date_created")
        created = parse_time(stamp)
        if created is None:
            return None  # an order with no creation time cannot be placed on the timeline
        note = str(node.get("customer_note") or "").strip()
        return cls(
            order_id=str(node["id"]),
            number=f"#{node.get('number') or node['id']}",
            status=str(node.get("status") or "pending"),
            created_at=created,
            paid_at=parse_time(_first_present(node, "date_paid_gmt", "date_paid")),
            total=_amount(node.get("total")),
            currency=str(node.get("currency") or "USD"),
            refunded_amount=sum(
                abs(_amount(refund.get("total")))
                for refund in node.get("refunds") or []
                if isinstance(refund, dict)
            ),
            customer_note=note or None,
            lines=tuple(
                OrderLine.from_row(row)
                for row in node.get("line_items") or []
                if isinstance(row, dict)
            ),
        )

    @property
    def is_open(self) -> bool:
        return self.status == AWAITING_FULFILMENT

    @property
    def counts_as_sale(self) -> bool:
        return self.status not in NOT_A_SALE

    @property
    def has_refund(self) -> bool:
        return self.status == "refunded" or self.refunded_amount > 0

    @property
    def units(self) -> int:
        quantities = (line.quantity for line in self.lines)
        return sum(quantities)

    def revenue_from(self, product_ids: frozenset[str]) -> float | None:
        """Summed ``line_items[].total`` for lines naming one of ``product_ids``; None when
        the order has no such line."""
        matched = [line.revenue for line in self.lines if line.product_id in product_ids]
        return sum(matched) if matched else None


@dataclass(frozen=True)
class DayRow:
    day: date
    orders: int
    sales: float


Orders = tuple[OrderRecord, ...]

# Two callers need the aggregations below: the async backend methods, which refresh the scan
# first, and the portal's synchronous handlers, which read the cached tuple directly. Plain
# functions over a tuple serve both.


class _DayTally:
    """Sales and order counts accumulated per calendar day."""

    def __init__(self) -> None:
        self._sales: defaultdict[date, float] = defaultdict(float)
        self._orders: Counter[date] = Counter()

    def add(self, day: date, amount: float) -> None:
        self._sales[day] += amount
        self._orders[day] += 1

    def row(self, day: date) -> DayRow:
        return DayRow(day=day, orders=self._orders[day], sales=round(self._sales[day], 2))


def _days_between(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def daily_rows(orders: Orders, start: date, end: date, product_ids: Subset = None) -> list[DayRow]:
    """Zero-filled ``DayRow`` per calendar day between ``start`` and ``end``. With
    ``product_ids`` given, a day's sales are the summed ``line_items[].total`` of matching
    lines, and an order counts once regardless of how many of its lines matched."""
    tally = _DayTally()
    for order in orders:
        day = order.created_at.date()
        if not order.counts_as_sale or day < start or day > end:
            continue
        amount = order.total if product_ids is None else order.revenue_from(product_ids)
        if amount is not None:
            tally.add(day, amount)
    return [tally.row(day) for day in _days_between(start, end)]


def _unit_tally(
    orders: Orders, days: int, include: Callable[[OrderRecord], bool]
) -> dict[str, int]:
    since = datetime.now(UTC) - timedelta(days=days)
    tally: Counter[str] = Counter()
    for order in orders:
        if order.created_at < since or not include(order):
            continue
        for line in order.lines:
            for key in line.keys:
                tally[key] += line.quantity
    return dict(tally)


def units_by_product(orders: Orders, days: int = 30) -> dict[str, int]:
    """Quantities sold in the last ``days``, keyed by product id and also by variation id
    wherever a line carries one."""
    return _unit_tally(orders, days, lambda order: order.counts_as_sale)


def refund_units_by_product(orders: Orders, days: int = 30) -> dict[str, int]:
    """Quantities on orders carrying at least one refund in the last ``days``. WooCommerce
    refunds attach to the order, not the line, so this is the nearest available proxy for
    returns per product."""
    return _unit_tally(orders, days, lambda order: order.has_refund)


@dataclass(frozen=True)
class _ScanLimits:
    """How far back, how many, and for how long the scan is trusted."""

    window_days: int
    max_orders: int
    page_size: int
    ttl_s: float


class OrderScan:
    """Cached read of the last ``window_days`` of orders, most recent first, refreshed once
    ``ttl_s`` seconds have passed."""

    def __init__(
        self,
        executor: WooExecutor,
        *,
        ttl_s: float = 60.0,
        window_days: int = 60,
        max_orders: int = 400,
        page_size: int = 100,
    ) -> None:
        self._limits = _ScanLimits(window_days, max_orders, page_size, ttl_s)
        self._executor = executor
        self._rows: Orders = ()
        # Monotonic deadline after which the next read reloads; zero means never loaded.
        self._fresh_until = 0.0

    @property
    def window_days(self) -> int:
        return self._limits.window_days

    def invalidate(self) -> None:
        self._fresh_until = 0.0

    def cached(self) -> Orders:
        return self._rows

    async def orders(self) -> Orders:
        if time.monotonic() > self._fresh_until:
            await self._reload()
        return self._rows

    def _query(self) -> dict[str, Any]:
        since = datetime.now(UTC) - timedelta(days=self._limits.window_days)
        return {
            "after": since.strftime(_GMT_FORMAT),
            "status": "any",
            "orderby": "date",
            "order": "desc",
        }

    async def _reload(self) -> None:
        nodes = await paged(
            self._executor,
            Routes.orders,
            self._query(),
            per_page=self._limits.page_size,
            max_items=self._limits.max_orders,
        )
        records: list[OrderRecord] = []
        for node in nodes:
            if isinstance(node, dict) and (record := OrderRecord.from_node(node)):
                records.append(record)
        records.sort(key=lambda record: record.created_at, reverse=True)
        self._rows = tuple(records)
        self._fresh_until = time.monotonic() + self._limits.ttl_s

    # The aggregation functions above, applied to a fresh scan.

    async def daily(self, start: date, end: date, product_ids: Subset = None) -> list[DayRow]:
        scanned = await self.orders()
        return daily_rows(scanned, start, end, product_ids)

    async def totals(self, start: date, end: date, product_ids: Subset = None) -> tuple[float, int]:
        rows = await self.daily(start, end, product_ids)
        sales = round(sum(row.sales for row in rows), 2)
        count = sum(row.orders for row in rows)
        return sales, count

    async def units_by_product(self, days: int = 30) -> dict[str, int]:
        scanned = await self.orders()
        return units_by_product(scanned, days)

    async def refund_units_by_product(self, days: int = 30) -> dict[str, int]:
        scanned = await self.orders()
        return refund_units_by_product(scanned, days)
