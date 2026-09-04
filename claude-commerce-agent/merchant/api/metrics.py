# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""Period parsing and the two metric sources behind ``get_business_snapshot`` and
``query_metrics``.

The preferred source is WooCommerce Analytics. ``GET wc-analytics/reports/revenue/stats``
returns ``totals`` and per-interval ``subtotals`` with ``total_sales``, ``net_revenue``,
``orders_count``, and ``avg_order_value``, which is very nearly a one-to-one fit. It
depends on the analytics lookup tables being populated (WooCommerce fills them through a
scheduled action after orders are placed) and on a credential allowed to
``view_woocommerce_reports``. If the first call fails, ``MetricsSource`` remembers that and
derives every later figure from the order scan; ``source_note`` records which path is in
use so the merchant context can say so.

Traffic and conversion are reported as None with an explanatory note. WooCommerce core
does not record sessions, and this deployment does not read a third-party analytics
plugin.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from demo_common.merchant_fixtures import change_pct
from merchant_agent import AlertCounts, BusinessSnapshot, MetricPoint, MetricSeries

from .catalog import CatalogCache, ProductRecord
from .orders import DayRow, OrderScan
from .rest_client import Routes, WooApiError, WooExecutor

logger = logging.getLogger(__name__)

TRAFFIC_NOTE = "traffic and conversion are unavailable: WooCommerce core records no sessions"

# Named windows: (length in days, days between the window's end and yesterday). Everything
# ends yesterday except ``last_week``, which is the seven days before the current seven.
_NAMED_WINDOWS: dict[str, tuple[int, int]] = {
    "today": (1, 0),
    "yesterday": (1, 0),
    "7d": (7, 0),
    "last_7_days": (7, 0),
    "last 7 days": (7, 0),
    "this_week": (7, 0),
    "last_week": (7, 7),
    "last_14_days": (14, 0),
    "30d": (30, 0),
    "last_30_days": (30, 0),
    "last 30 days": (30, 0),
    "this_month": (30, 0),
    "last_month": (30, 0),
    "90d": (90, 0),
    "last_90_days": (90, 0),
    "last 90 days": (90, 0),
    "quarter": (90, 0),
}

# Metric name -> the ``subtotals`` column WooCommerce Analytics reports it under.
_ANALYTICS_COLUMN = {
    "sales": "total_sales",
    "revenue": "total_sales",
    "net_revenue": "net_revenue",
    "orders": "orders_count",
    "average_order_value": "avg_order_value",
    "aov": "avg_order_value",
}
_MONEY_METRICS = frozenset({"sales", "revenue", "average_order_value", "aov"})
_SESSION_METRICS = frozenset({"traffic", "sessions", "conversion", "conversion_rate", "visits"})
_BUCKET_DAYS = {"day": 1, "week": 7, "month": 30}


@dataclass(frozen=True)
class Period:
    """Inclusive range of calendar days. The end is never later than yesterday: today's
    partial total placed next to full days would look like a drop in sales."""

    start: date
    end: date

    @classmethod
    def ending(cls, end: date, days: int) -> Period:
        return cls(start=end - timedelta(days=days - 1), end=end)

    @property
    def label(self) -> str:
        return f"{self.start.isoformat()}/{self.end.isoformat()}"

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def previous(self) -> Period:
        return Period.ending(self.start - timedelta(days=1), self.days)

    def analytics_bounds(self) -> dict[str, str]:
        return {
            "after": f"{self.start.isoformat()}T00:00:00",
            "before": f"{self.end.isoformat()}T23:59:59",
        }


def _explicit_range(text: str, latest: date) -> Period | None:
    """``YYYY-MM-DD/YYYY-MM-DD`` as a Period clipped to ``latest``; None when malformed or
    inverted."""
    first, _, second = text.partition("/")
    try:
        start = date.fromisoformat(first.strip())
        end = min(date.fromisoformat(second.strip()), latest)
    except ValueError:
        return None
    return Period(start=start, end=end) if start <= end else None


def resolve_period(raw: str | None, *, default_days: int = 7, today: date | None = None) -> Period:
    """Turn a period string into a ``Period``: ``YYYY-MM-DD/YYYY-MM-DD``, one of the named
    windows in ``_NAMED_WINDOWS``, or ``default_days`` ending yesterday when the text is
    empty or unrecognised."""
    yesterday = (today or datetime.now(UTC).date()) - timedelta(days=1)
    text = (raw or "").strip().lower()
    if "/" in text and (explicit := _explicit_range(text, yesterday)) is not None:
        return explicit
    length, lag = _NAMED_WINDOWS.get(text, (default_days, 0))
    return Period.ending(yesterday - timedelta(days=lag), length)


def canonical_metric(metric: str) -> str:
    return "_".join(metric.strip().lower().split(" "))


def _reading(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class _AnalyticsReport:
    """Access to ``wc-analytics/reports/revenue/stats`` with a sticky availability flag:
    the first refusal or malformed answer switches it off for the life of the object."""

    def __init__(self, executor: WooExecutor, enabled: bool) -> None:
        self._executor = executor
        self.enabled = enabled
        self.answered: bool | None = None if enabled else False

    @property
    def in_use(self) -> bool:
        return bool(self.answered)

    async def fetch(self, period: Period, interval: str | None = None) -> dict[str, Any] | None:
        if self.answered is False:
            return None
        params: dict[str, Any] = {**period.analytics_bounds(), "per_page": 100, "order": "asc"}
        if interval:
            params["interval"] = interval
        try:
            body, _ = await self._executor.request("GET", Routes.revenue_stats, params=params)
        except WooApiError as error:
            logger.info("revenue stats unavailable, using the order scan instead: %s", error)
            body = None
        usable = isinstance(body, dict) and isinstance(body.get("totals"), dict)
        self.answered = usable
        return body if usable else None

    async def totals(self, period: Period) -> tuple[float, int] | None:
        """``(sales, orders)`` for the period, or None when the report cannot supply
        both."""
        report = await self.fetch(period)
        if report is None:
            return None
        figures = report["totals"]
        sales = _reading(figures.get("total_sales"))
        if sales is None:
            sales = _reading(figures.get("net_revenue"))
        orders = _reading(figures.get("orders_count"))
        if sales is None or orders is None:
            return None
        return round(sales, 2), int(orders)

    async def points(self, column: str, period: Period, interval: str) -> list[MetricPoint]:
        """One point per reported interval for ``column``; empty when the report is
        unavailable or carries no usable rows."""
        report = await self.fetch(period, interval)
        series: list[MetricPoint] = []
        for row in (report or {}).get("intervals") or []:
            if not isinstance(row, dict):
                continue
            value = _reading((row.get("subtotals") or {}).get(column))
            day = str(row.get("date_start") or row.get("interval") or "")[:10]
            if value is not None and day:
                series.append(MetricPoint(date=day, value=round(value, 2)))
        return series


def _bucket_value(name: str, sales: float, orders: int) -> float:
    if name == "orders":
        return float(orders)
    if name in {"average_order_value", "aov"}:
        return round(sales / orders, 2) if orders else 0.0
    return sales


def _fold(rows: list[DayRow], span: int, name: str) -> list[MetricPoint]:
    """Collapse daily rows into ``span``-day buckets, each stamped with its first day."""
    series: list[MetricPoint] = []
    for offset in range(0, len(rows), span):
        bucket = rows[offset : offset + span]
        sales = round(sum(row.sales for row in bucket), 2)
        orders = sum(row.orders for row in bucket)
        series.append(
            MetricPoint(date=bucket[0].day.isoformat(), value=_bucket_value(name, sales, orders))
        )
    return series


class MetricsSource:
    """Snapshot and series computations. Tries WooCommerce Analytics first; after one
    failure it stays on the order-scan path for the life of the object."""

    def __init__(
        self,
        executor: WooExecutor,
        *,
        order_scan: OrderScan,
        catalog: CatalogCache,
        currency: str,
        analytics_enabled: bool = True,
    ) -> None:
        self._analytics = _AnalyticsReport(executor, analytics_enabled)
        self._orders = order_scan
        self._catalog = catalog
        self._currency = currency

    @property
    def source_note(self) -> str:
        """Text for the ``data_source`` entry in the merchant context, naming the active
        path and the traffic figures that are missing."""
        if self._analytics.in_use:
            source = "figures from WooCommerce Analytics (wc-analytics/reports/revenue/stats)"
        else:
            why = "unavailable" if self._analytics.enabled else "disabled by configuration"
            source = f"figures computed from the trailing order scan; WooCommerce Analytics {why}"
        return f"{source}; {TRAFFIC_NOTE}"

    # -- Snapshot ------------------------------------------------------------------

    async def _totals(self, period: Period) -> tuple[float, int]:
        reported = await self._analytics.totals(period)
        if reported is not None:
            return reported
        return await self._orders.totals(period.start, period.end)

    async def snapshot(self, period_text: str | None, alerts: AlertCounts) -> BusinessSnapshot:
        period = resolve_period(period_text, default_days=7)
        earlier = period.previous()
        sales, orders = await self._totals(period)
        prior_sales, prior_orders = await self._totals(earlier)
        return BusinessSnapshot(
            period=period.label,
            compare_to=earlier.label,
            sales=sales,
            orders=orders,
            traffic=None,
            conversion_rate=None,
            average_order_value=round(sales / orders, 2) if orders else 0.0,
            sales_change_pct=change_pct(sales, prior_sales),
            orders_change_pct=change_pct(orders, prior_orders),
            traffic_change_pct=None,
            conversion_change_pct=None,
            currency=self._currency,
            alerts=alerts,
            note=TRAFFIC_NOTE,
        )

    # -- Series --------------------------------------------------------------------

    async def series(
        self,
        metric: str,
        period_text: str | None,
        granularity: str,
        segment: str | None,
    ) -> MetricSeries:
        name = canonical_metric(metric)
        period = resolve_period(period_text, default_days=30)
        bucket = granularity if granularity in _BUCKET_DAYS else "day"
        segment_label = (segment or "").strip().lower() or None
        shape: dict[str, Any] = {
            "metric": name,
            "granularity": bucket,
            "period": period.label,
            "segment": segment_label,
        }
        if name in _SESSION_METRICS:
            return MetricSeries(**shape, points=[], note=TRAFFIC_NOTE)
        points = await self._points(name, period, bucket, segment_label)
        unit = self._currency if name in _MONEY_METRICS else None
        return MetricSeries(**shape, unit=unit, points=points)

    async def _points(
        self, name: str, period: Period, bucket: str, segment: str | None
    ) -> list[MetricPoint]:
        # Analytics can only answer for the whole store; a segment always goes through the
        # order lines, which carry product ids.
        column = _ANALYTICS_COLUMN.get(name)
        if column and segment is None:
            reported = await self._analytics.points(column, period, bucket)
            if reported:
                return reported
        product_ids = await self._segment_products(segment) if segment else None
        rows = await self._orders.daily(period.start, period.end, product_ids)
        return _fold(rows, _BUCKET_DAYS[bucket], name)

    async def _segment_products(self, segment: str) -> frozenset[str]:
        """Product ids for a segment label, matched loosely against category names, slug,
        and title, so "workshop tools" and "workshop-tools" both find the category."""
        wanted = segment.replace("-", " ").replace("_", " ").strip()

        def names_of(record: ProductRecord) -> tuple[str, ...]:
            return (
                " ".join(record.categories).lower(),
                record.slug.replace("-", " "),
                record.title.lower(),
            )

        return frozenset(
            record.product_id
            for record in await self._catalog.all()
            if any(wanted in name for name in names_of(record))
        )
