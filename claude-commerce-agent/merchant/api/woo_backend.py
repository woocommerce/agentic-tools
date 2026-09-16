# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""``WooMerchantBackend``: the ``MerchantBackend`` interface implemented against
WooCommerce's REST API.

Everything WooCommerce-specific on the merchant side lives in this package. The agent's
prompt, tools, skills, and approval gates come from Anthropic's commerce-agents reference
unchanged; this class and its helper modules translate the sixteen backend methods into
``wc/v3`` and ``wc-analytics`` calls.

Decisions made where the interface and WooCommerce do not line up. Each one narrows the
mapping to what the store actually records instead of filling the gap with a guess:

- Listing ids are WordPress post ids rendered as strings. A variable product maps to a
  family with one variant per variation. Products and variations share one post id
  sequence, so a variation id is accepted anywhere a listing id is.
- ``price`` is ``regular_price``. An active ``sale_price`` is surfaced in ``attributes``
  rather than replacing it, and a promotion is implemented as a dated sale price, the one
  write for which WooCommerce has a native object.
- ``paused`` corresponds to post status ``draft`` (also ``pending`` and ``private``),
  which is what the portal's pause action writes. Products in ``trash`` are never loaded.
- ``unit_cost`` is read from WooCommerce's cost-of-goods data when the store has it;
  otherwise margins are None instead of estimated.
- Traffic and conversion are None. WooCommerce core keeps no session data.
- Campaigns are read-only. ``wc-admin/marketing/campaigns`` lists what the store's
  marketing extensions report (core has no campaigns of its own), and WooCommerce has no
  API to create one, so ``stage_campaign`` refuses and says why.
- ``execute_analysis_query`` is left at the base class default: no SQL surface is exposed.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from demo_common.merchant_fixtures import change_pct, filter_listings, margin_pct
from merchant_agent import (
    ActorKind,
    BusinessSnapshot,
    Campaign,
    CampaignDraft,
    ChangeItem,
    ChangeKind,
    ChangeLedger,
    ChangeStatus,
    DataLimitation,
    InventoryActionItem,
    InventoryAlert,
    Listing,
    ListingDetails,
    ListingFilters,
    MerchantAgentConfig,
    MerchantBackend,
    MerchantSessionContext,
    MetricSeries,
    OrderIssue,
    PriceUpdateItem,
    PricingContext,
    PromotionDraft,
    StagedChange,
)
from merchant_agent.changes import ChangeNotApplicable, GuardrailViolation, check_guardrails

from .agent_config import DATA_DIR, WooSettings
from .alerts import AlertBuilder, AlertRules
from .catalog import CatalogCache, ListingTarget, strip_html
from .metrics import TRAFFIC_NOTE, MetricsSource, resolve_period
from .orders import DayRow, OrderScan, daily_rows
from .rest_client import Routes, WooApiError, WooExecutor, paged
from .staging import (
    LISTING_FIELDS,
    PRICE_FIELD,
    SALE_PRICE_FIELD,
    STATUS_FIELD,
    STOCK_FIELD,
    WooWriter,
    WriteFailed,
)

NO_CAMPAIGNS = (
    "this store lists no campaigns: WooCommerce shows only the campaigns its installed "
    "marketing extensions report, and none reports any"
)
CAMPAIGNS_READ_ONLY = (
    "campaigns are read from the store's marketing extensions and cannot be created or "
    "changed here; WooCommerce has no API to write one"
)
# At most 140 characters: it travels as a DataLimitation note.
CAMPAIGN_FIGURES = (
    "campaigns are read-only, from installed marketing extensions; budget, dates, and status "
    "are not reported, so budget reads 0"
)
SALES_WINDOW_DAYS = 30
DEMAND_WINDOW_DAYS = 14
DEMAND_SHIFT_PCT = 15
REVIEW_SNIPPET_CHARS = 200
TREND_DAYS = 30


@dataclass(frozen=True)
class StoreProfile:
    name: str
    url: str
    currency: str
    timezone: str | None
    woocommerce_version: str | None


class _MarginPreview:
    """Accumulates the margin figures shown on a staged change card: the weekly revenue
    impact at the recent sales pace, and a before/after margin line for each target whose
    unit cost is known. The single-target percentages are only reported when the change
    has exactly one priced item, since one pair of numbers cannot describe several."""

    def __init__(self) -> None:
        self.impact = 0.0
        self.notes: list[str] = []
        self._pairs: list[tuple[float, float]] = []

    def add(
        self,
        target: ListingTarget,
        *,
        before: float,
        after: float,
        daily_units: float,
        wording: str,
    ) -> None:
        self.impact += (after - before) * daily_units * 7
        cost = target.priced.unit_cost
        if cost and after > 0 and before > 0:
            was, now = margin_pct(before, cost), margin_pct(after, cost)
            self._pairs.append((was, now))
            self.notes.append(f"{target.title}: {wording} {was}% to {now}% ({now - was:+.1f} pts)")

    def sale_price_warning(self, target: ListingTarget) -> None:
        priced = target.priced
        if priced.on_sale and priced.sale_price is not None:
            self.notes.append(
                f"{target.title} currently has a sale price of {priced.sale_price:.2f}; this "
                "change sets regular_price only, and buyers pay the sale price until its window "
                "closes"
            )

    @property
    def before_pct(self) -> float | None:
        return self._pairs[0][0] if len(self._pairs) == 1 else None

    @property
    def after_pct(self) -> float | None:
        return self._pairs[0][1] if len(self._pairs) == 1 else None

    def ledger_fields(self) -> dict[str, Any]:
        return {
            "margin_impact": round(self.impact, 2),
            "margin_before_pct": self.before_pct,
            "margin_after_pct": self.after_pct,
        }


@dataclass(frozen=True)
class _InsightCard:
    """One suggestion card on the portal overview, rendered from whichever alerts, issues,
    or pending changes it selects."""

    insight_id: str
    kind: str
    pick: Callable[[dict[str, Sequence[Any]]], Sequence[Any]]
    headline: Callable[[Sequence[Any]], str]
    detail: Callable[[Sequence[Any]], str]
    prompt: Callable[[Sequence[Any]], str]

    def render(self, hits: Sequence[Any]) -> dict[str, Any]:
        return {
            "insight_id": self.insight_id,
            "kind": self.kind,
            "headline": self.headline(hits),
            "detail": self.detail(hits),
            "prompt": self.prompt(hits),
        }


def _alerts_of(kind: str) -> Callable[[dict[str, Sequence[Any]]], list[InventoryAlert]]:
    return lambda pools: [alert for alert in pools["inventory"] if alert.kind == kind]


def _issues_of(kind: str) -> Callable[[dict[str, Sequence[Any]]], list[OrderIssue]]:
    return lambda pools: [issue for issue in pools["issues"] if issue.kind == kind]


def _low_stock_headline(low: Sequence[InventoryAlert]) -> str:
    if len(low) > 1:
        return f"{len(low)} listing(s) have hit their low-stock threshold"
    return f"{low[0].title} has {low[0].stock} units left"


_INSIGHT_CARDS: tuple[_InsightCard, ...] = (
    _InsightCard(
        "low-stock",
        "inventory",
        _alerts_of("low_stock"),
        headline=_low_stock_headline,
        detail=lambda low: ", ".join(f"{a.title} ({a.stock} left)" for a in low[:3]),
        prompt=lambda _: (
            "Which low-stock listings need a restock first, and how many units should I order "
            "for each?"
        ),
    ),
    _InsightCard(
        "slow-movers",
        "inventory",
        _alerts_of("slow_mover"),
        headline=lambda slow: f"{len(slow)} listing(s) have stock that is not selling",
        detail=lambda slow: ", ".join(
            f"{a.title} ({a.stock} in stock, {a.sales_last_30d or 0} sold in 30d)" for a in slow[:3]
        ),
        prompt=lambda _: (
            "Which of my slow movers deserve a promotion or a content refresh, and which should "
            "I simply sell through?"
        ),
    ),
    _InsightCard(
        "delayed-orders",
        "orders",
        _issues_of("delayed"),
        headline=lambda late: f"{len(late)} order(s) are overdue for fulfilment",
        detail=lambda late: late[0].summary,
        prompt=lambda _: "Go through the overdue orders with me and suggest a next step for each.",
    ),
    _InsightCard(
        "buyer-messages",
        "orders",
        _issues_of("buyer_message"),
        headline=lambda notes: f"{len(notes)} open order(s) include a customer note",
        detail=lambda notes: notes[0].summary,
        prompt=lambda _: (
            "What did customers write on their open orders, and what action does each note "
            "call for?"
        ),
    ),
    _InsightCard(
        "return-spikes",
        "returns",
        _issues_of("return_spike"),
        headline=lambda spikes: f"{len(spikes)} listing(s) have a refund rate over the threshold",
        detail=lambda spikes: spikes[0].summary,
        prompt=lambda spikes: (
            f"{spikes[0].summary} — what might be causing this, and what would you fix in the "
            "listing?"
        ),
    ),
    _InsightCard(
        "pending-changes",
        "changes",
        lambda pools: list(pools["pending"]),
        headline=lambda pending: f"{len(pending)} staged change(s) need your approval",
        detail=lambda pending: pending[0].summary,
        prompt=lambda _: (
            "Give me a rundown of the changes awaiting my approval and the effect each one "
            "would have."
        ),
    ),
)

# Portal sparklines: series name -> value per day. No conversion series is produced; with
# no sessions recorded, a flat zero line would look like a measured value.
_TREND_VALUES: dict[str, Callable[[DayRow], float]] = {
    "sales": lambda row: row.sales,
    "orders": lambda row: float(row.orders),
    "average_order_value": lambda row: round(row.sales / row.orders, 2) if row.orders else 0.0,
}


@dataclass
class _StoreState:
    """Handles the backend keeps for one store. Grouped so the constructor reads as a
    list of collaborators rather than a run of assignments."""

    executor: WooExecutor
    settings: WooSettings
    config: MerchantAgentConfig
    profile: StoreProfile | None = None
    metrics: MetricsSource | None = None
    promotion_windows: dict[str, dict[str, Any]] = field(default_factory=dict)


class WooMerchantBackend(MerchantBackend):
    """A single WooCommerce store behind the ``MerchantBackend`` interface."""

    def __init__(
        self,
        executor: WooExecutor,
        settings: WooSettings,
        config: MerchantAgentConfig,
    ) -> None:
        self._state = _StoreState(executor, settings, config)
        self.ledger = ChangeLedger(config)
        self.catalog = CatalogCache(executor)
        self.orders = OrderScan(executor)
        rules = AlertRules.load(
            DATA_DIR / "thresholds.json",
            low_stock_default=settings.low_stock_default,
            fulfilment_sla_days=settings.fulfilment_sla_days,
        )
        self.alerts = AlertBuilder(self.catalog, self.orders, rules)
        self._writer = WooWriter(executor, self.catalog)

    @property
    def _executor(self) -> WooExecutor:
        return self._state.executor

    @property
    def _settings(self) -> WooSettings:
        return self._state.settings

    @property
    def _config(self) -> MerchantAgentConfig:
        return self._state.config

    @property
    def promotion_windows(self) -> dict[str, dict[str, Any]]:
        """Start and end dates for each staged promotion, keyed by change id. See
        ``stage_promotion`` for why they are not stored on the change itself."""
        return self._state.promotion_windows

    # -- Store profile ---------------------------------------------------------------

    async def _site_index(self) -> dict[str, Any]:
        body, _ = await self._executor.request("GET", Routes.site_index)
        return body if isinstance(body, dict) else {}

    async def _system_status(self) -> dict[str, Any]:
        try:
            body, _ = await self._executor.request("GET", Routes.system_status)
        except WooApiError:
            return {}
        return body if isinstance(body, dict) else {}

    async def profile(self) -> StoreProfile:
        """Store identity from two reads, cached for the process: the WordPress root index
        (``GET /wp-json/``) for name, home URL, and timezone, and ``wc/v3/system_status``
        for currency and WooCommerce version. ``WOOCOMMERCE_STORE_NAME`` overrides the site
        title when set."""
        if self._state.profile is None:
            index = await self._site_index()
            status = await self._system_status()
            settings = self._settings
            self._state.profile = StoreProfile(
                name=settings.store_name or index.get("name") or settings.merchant_id,
                url=index.get("home") or index.get("url") or settings.store_url,
                currency=(status.get("settings") or {}).get("currency") or "USD",
                timezone=index.get("timezone_string") or None,
                woocommerce_version=(status.get("environment") or {}).get("version"),
            )
        return self._state.profile

    @property
    def store_name(self) -> str:
        if self._state.profile is not None:
            return self._state.profile.name
        return self._settings.store_name or self._settings.merchant_id

    @property
    def display_currency(self) -> str:
        return self._state.profile.currency if self._state.profile is not None else "USD"

    async def currency(self) -> str:
        return (await self.profile()).currency

    async def metrics(self) -> MetricsSource:
        if self._state.metrics is None:
            self._state.metrics = MetricsSource(
                self._executor,
                order_scan=self.orders,
                catalog=self.catalog,
                currency=await self.currency(),
                analytics_enabled=self._settings.analytics_enabled,
            )
        return self._state.metrics

    # -- Performance -------------------------------------------------------------------

    async def get_business_snapshot(
        self, session: MerchantSessionContext, period: str | None = None
    ) -> BusinessSnapshot:
        counts = await self.alerts.counts(len(self.ledger.pending()))
        return await (await self.metrics()).snapshot(period, counts)

    async def query_metrics(
        self,
        session: MerchantSessionContext,
        metric: str,
        period: str | None = None,
        granularity: str = "day",
        segment: str | None = None,
    ) -> MetricSeries:
        return await (await self.metrics()).series(metric, period, granularity, segment)

    async def get_campaign_performance(
        self, session: MerchantSessionContext, campaign_id: str | None = None
    ) -> list[Campaign]:
        """The campaigns the store's marketing extensions report through
        ``wc-admin/marketing/campaigns``. Core only lists them: a store with no marketing
        extension lists none, which is reported as an absence rather than as an empty
        result, because "no campaigns" and "no campaign data" lead to different advice."""
        currency = await self.currency()
        try:
            rows = await paged(
                self._executor, Routes.marketing_campaigns, per_page=100, max_items=100
            )
        except WooApiError as error:
            raise ChangeNotApplicable(
                f"campaigns cannot be read: the store refused the marketing campaigns route ({error})"
            ) from error
        campaigns = [campaign_from_node(row, currency) for row in rows if isinstance(row, dict)]
        if not campaigns:
            raise ChangeNotApplicable(NO_CAMPAIGNS)
        if campaign_id:
            return [entry for entry in campaigns if entry.campaign_id == campaign_id]
        return campaigns

    # -- Catalog -----------------------------------------------------------------------

    async def search_listings(
        self,
        session: MerchantSessionContext,
        query: str,
        filters: ListingFilters | None = None,
        limit: int = 8,
    ) -> list[Listing]:
        currency = await self.currency()
        found = await self.catalog.search(query, limit * 3)
        if not found and query.strip().isdigit():
            direct = await self.catalog.get(query)
            found = [direct] if direct else []
        units = await self.orders.units_by_product(days=SALES_WINDOW_DAYS)
        return filter_listings(
            [record.to_listing(currency) for record in found],
            filters,
            limit,
            sales_of=lambda listing_id: units.get(listing_id, 0),
        )

    async def _sales_and_returns(self, listing_id: str) -> tuple[int, float | None]:
        """Units sold in the last 30 days and the share of them that came back as refunds,
        None when nothing sold."""
        sold = (await self.orders.units_by_product(days=SALES_WINDOW_DAYS)).get(listing_id, 0)
        refunds = await self.orders.refund_units_by_product(days=SALES_WINDOW_DAYS)
        rate = round(refunds.get(listing_id, 0) / sold * 100, 1) if sold else None
        return sold, rate

    async def get_listing(
        self, session: MerchantSessionContext, listing_id: str
    ) -> ListingDetails | None:
        target = await self.catalog.target(listing_id)
        if target is None:
            return None
        currency = await self.currency()
        sold, return_rate = await self._sales_and_returns(target.listing_id)
        if target.variant is not None:
            return ListingDetails(
                **target.record.variant_listing(target.variant, currency).model_dump(),
                long_description=target.record.description,
                sales_last_30d=sold,
                return_rate_pct=return_rate,
            )
        return target.record.to_details(
            currency,
            sales_last_30d=sold,
            return_rate_pct=return_rate,
            review_snippets=await self._review_snippets(target.record.product_id),
        )

    async def _review_snippets(self, product_id: str, limit: int = 3) -> list[str]:
        """Up to ``limit`` approved reviews from ``wc/v3/products/reviews``, HTML stripped.
        Review text is customer-written and gets fenced by the tool executor before the
        model reads it. A credential that cannot list reviews, or a store with reviews
        switched off, yields an empty list."""
        try:
            body, _ = await self._executor.request(
                "GET",
                Routes.reviews,
                params={"product": product_id, "per_page": limit, "status": "approved"},
            )
        except WooApiError:
            return []
        reviews = body if isinstance(body, list) else []
        texts = (strip_html(review.get("review")) for review in reviews if isinstance(review, dict))
        return [text[:REVIEW_SNIPPET_CHARS] for text in texts if text]

    def all_listings(self) -> list[Listing]:
        """Full catalog for the portal's listings page. The shared router calls this
        synchronously, so it maps the cached records instead of fetching."""
        currency = self.display_currency
        return [record.to_listing(currency) for record in self.catalog.cached()]

    # -- Inventory and order health ------------------------------------------------------

    async def get_inventory_alerts(self, session: MerchantSessionContext) -> list[InventoryAlert]:
        return await self.alerts.inventory_alerts()

    async def get_order_issues(self, session: MerchantSessionContext) -> list[OrderIssue]:
        return await self.alerts.order_issues()

    # -- Pricing --------------------------------------------------------------------------

    async def get_pricing_context(
        self, session: MerchantSessionContext, listing_id: str
    ) -> PricingContext | None:
        target = await self.catalog.target(listing_id)
        if target is None:
            return None
        currency = await self.currency()
        if not target.needs_split:
            return await self._pricing(target, currency)
        # A variable parent has no price of its own: report each variation and the lowest
        # of their prices as the headline figure.
        record = target.record
        per_variation = [
            await self._pricing(ListingTarget(record, variant), currency)
            for variant in record.variants
        ]
        return PricingContext(
            listing_id=record.product_id,
            current_price=min(
                (entry.current_price for entry in per_variation), default=record.listing_price
            ),
            currency=currency,
            max_price_delta_pct=self._config.max_price_delta_pct,
            max_promotion_discount_pct=self._config.max_promotion_discount_pct,
            demand_signal=await self._demand_signal(record.product_id),
            last_changed=_day_of(record.updated_at),
            variants=per_variation,
        )

    async def _pricing(self, target: ListingTarget, currency: str) -> PricingContext:
        priced = target.priced
        price, cost = priced.regular_price, priced.unit_cost
        cap = self._config.max_price_delta_pct
        policy_floor = round(price * (1 - cap / 100), 2)
        # The floor is the configured delta cap, but never below unit cost when the store
        # records one: a cut the guardrails accept could still sell at a loss.
        floor = max(policy_floor, round(cost, 2)) if cost else policy_floor
        return PricingContext(
            listing_id=target.listing_id,
            current_price=price,
            currency=currency,
            unit_cost=cost,
            margin_pct=margin_pct(price, cost) if cost and price > 0 else None,
            min_price=floor,
            max_price=round(price * (1 + cap / 100), 2),
            max_price_delta_pct=cap,
            max_promotion_discount_pct=self._config.max_promotion_discount_pct,
            min_price_basis="cost" if cost and cost > policy_floor else "policy",
            demand_signal=await self._demand_signal(target.listing_id),
            last_changed=_day_of(priced.updated_at),
            option_values=dict(target.variant.option_values) if target.variant else {},
        )

    async def _demand_signal(self, listing_id: str) -> str | None:
        """Compare units sold in the last 14 days with the 14 before them. Returns None when
        both windows are zero; a product with no sales at all is not "steady"."""
        recent = (await self.orders.units_by_product(days=DEMAND_WINDOW_DAYS)).get(listing_id, 0)
        both = (await self.orders.units_by_product(days=DEMAND_WINDOW_DAYS * 2)).get(listing_id, 0)
        earlier = both - recent
        if recent == earlier == 0:
            return None
        shift = change_pct(recent, earlier)
        if shift is None:
            return "rising" if recent else "falling"
        if shift > DEMAND_SHIFT_PCT:
            return "rising"
        return "falling" if shift < -DEMAND_SHIFT_PCT else "steady"

    # -- Staged writes ---------------------------------------------------------------------

    async def _target(self, listing_id: str) -> ListingTarget:
        """Resolve ``listing_id`` or raise ``ChangeNotApplicable``. Unknown ids are rejected
        at staging time so no change can reach the approval card pointing at nothing."""
        target = await self.catalog.target(listing_id)
        if target is None:
            raise ChangeNotApplicable(f"no product or variation with id {listing_id} in this store")
        return target

    async def _sales_pace(self) -> Callable[[str], float]:
        """Units per day at the trailing 30-day rate, by listing id."""
        units = await self.orders.units_by_product(days=SALES_WINDOW_DAYS)
        return lambda listing_id: units.get(listing_id, 0) / SALES_WINDOW_DAYS

    def _stage(
        self, kind: ChangeKind, session: MerchantSessionContext, **fields: Any
    ) -> StagedChange:
        return self.ledger.stage(
            kind=kind, actor=session.operator, actor_kind=ActorKind.AGENT, **fields
        )

    async def stage_listing_update(
        self,
        session: MerchantSessionContext,
        listing_id: str,
        fields: dict[str, Any],
        note: str | None = None,
    ) -> StagedChange:
        target = await self._target(listing_id)
        record = target.record
        if target.is_variation:
            raise ChangeNotApplicable(
                f"name, descriptions, and category belong to the parent product "
                f"{record.product_id} and apply to all its variations; stage the edit against "
                f"{record.product_id}"
            )
        # Validate writable fields at staging time. Otherwise an unsupported field would
        # produce a preview card the operator can approve, and only the apply step would
        # fail. Fields the config routes to a dedicated tool (blocked fields) are left for
        # the guardrail check, which produces the message naming that tool.
        routed_elsewhere = {name.casefold() for name in self._config.listing_update_blocked_fields}
        unwritable = sorted(
            name
            for name in fields
            if name not in LISTING_FIELDS and name.casefold() not in routed_elsewhere
        )
        if unwritable:
            raise ChangeNotApplicable(
                f"this deployment does not write {', '.join(unwritable)} on a listing; the "
                f"writable fields are {', '.join(sorted(LISTING_FIELDS))}"
            )
        listing = record.to_listing(await self.currency())
        current: dict[str, Any] = {
            "title": listing.title,
            "description": record.description,
            "long_description": record.description,
            "short_description": record.short_description,
            "category": listing.category,
            "sku": record.sku,
            "slug": record.slug,
        }
        return self._stage(
            ChangeKind.LISTING_UPDATE,
            session,
            summary=note or f"Update listing content on {record.title}",
            items=[
                ChangeItem(
                    target=record.product_id, field=name, before=current.get(name), after=value
                )
                for name, value in fields.items()
            ],
        )

    async def stage_price_update(
        self,
        session: MerchantSessionContext,
        items: list[PriceUpdateItem],
        note: str | None = None,
    ) -> StagedChange:
        pace = await self._sales_pace()
        preview = _MarginPreview()
        staged: list[ChangeItem] = []
        for item in items:
            target = await self._target(item.listing_id)
            if target.needs_split:
                raise ChangeNotApplicable(
                    f"{target.record.title} has {len(target.record.variants)} priced variations; "
                    f"price each variation by its own id: {', '.join(target.variation_ids)}"
                )
            before = target.priced.regular_price
            preview.add(
                target,
                before=before,
                after=item.new_price,
                daily_units=pace(target.listing_id),
                wording="margin moves from",
            )
            preview.sale_price_warning(target)
            staged.append(
                ChangeItem(
                    target=target.listing_id, field=PRICE_FIELD, before=before, after=item.new_price
                )
            )
        return self._stage(
            ChangeKind.PRICE_UPDATE,
            session,
            summary=note or f"Price update for {len(items)} listing(s)",
            items=staged,
            currency=await self.currency(),
            guardrail_notes=preview.notes or None,
            **preview.ledger_fields(),
        )

    async def stage_inventory_action(
        self,
        session: MerchantSessionContext,
        items: list[InventoryActionItem],
        note: str | None = None,
    ) -> StagedChange:
        staged: list[ChangeItem] = []
        notes: list[str] = []
        for item in items:
            target = await self._target(item.listing_id)
            if item.action == "restock":
                staged.append(self._restock_item(target, item.quantity or 0))
                continue
            wanted = "paused" if item.action == "pause" else "active"
            staged.append(
                ChangeItem(
                    target=target.listing_id,
                    field=STATUS_FIELD,
                    before=self._listing_status(target),
                    after=wanted,
                )
            )
            if wanted == "paused":
                post_status = "private" if target.is_variation else "draft"
                notes.append(
                    f"pausing {target.title} sets its WooCommerce status to {post_status}; it "
                    "disappears from the storefront until it is activated again"
                )
        return self._stage(
            ChangeKind.INVENTORY_ACTION,
            session,
            summary=note or f"Inventory action for {len(items)} listing(s)",
            items=staged,
            guardrail_notes=notes or None,
        )

    @staticmethod
    def _restock_item(target: ListingTarget, quantity: int) -> ChangeItem:
        if target.needs_split:
            raise ChangeNotApplicable(
                f"{target.record.title} tracks stock on each variation; restock each variation "
                f"by its own id: {', '.join(target.variation_ids)}"
            )
        priced = target.priced
        if not priced.manage_stock:
            raise ChangeNotApplicable(
                f"{target.title} does not track inventory (manage_stock is false in "
                "WooCommerce), so there is no quantity to add to"
            )
        on_hand = priced.stock_quantity or 0
        return ChangeItem(
            target=target.listing_id, field=STOCK_FIELD, before=on_hand, after=on_hand + quantity
        )

    @staticmethod
    def _listing_status(target: ListingTarget) -> str:
        if target.variant is None:
            return target.record.status
        # Currency is irrelevant to the status; any code gives the same answer.
        return target.record.variant_listing(target.variant, "USD").status

    async def stage_promotion(
        self, session: MerchantSessionContext, promotion: PromotionDraft
    ) -> StagedChange:
        """A promotion becomes a dated sale: WooCommerce applies ``sale_price`` on its own
        between ``date_on_sale_from`` and ``date_on_sale_to``. Naming a variable product
        expands to one item per variation, and each item counts toward the configured
        items-per-change limit. A negative discount is rejected because WooCommerce has no
        scheduled form of a price increase."""
        if promotion.discount_pct < 0:
            raise ChangeNotApplicable(
                "A WooCommerce sale can only lower a price for a period; there is no dated form "
                "of a temporary increase. Stage a price update instead"
            )
        targets: list[ListingTarget] = []
        for listing_id in promotion.listing_ids:
            named = await self._target(listing_id)
            if named.needs_split:
                targets += [ListingTarget(named.record, v) for v in named.record.variants]
            else:
                targets.append(named)
        pace = await self._sales_pace()
        preview = _MarginPreview()
        staged: list[ChangeItem] = []
        keep = 1 - promotion.discount_pct / 100
        for target in targets:
            full = target.priced.regular_price
            sale = round(full * keep, 2)
            preview.add(
                target,
                before=full,
                after=sale,
                daily_units=pace(target.listing_id),
                wording="margin during the sale window moves from",
            )
            staged.append(
                ChangeItem(
                    target=target.listing_id, field=SALE_PRICE_FIELD, before=full, after=sale
                )
            )
        notes = [
            *preview.notes,
            f"on approval each listing gets a scheduled sale price running {promotion.starts} "
            f"to {promotion.ends}; WooCommerce switches it on and off on those dates",
        ]
        change = self._stage(
            ChangeKind.PROMOTION,
            session,
            summary=(
                f"{promotion.name} ({promotion.discount_pct:.0f}% off, "
                f"{promotion.starts} to {promotion.ends})"
            ),
            items=staged,
            currency=await self.currency(),
            guardrail_notes=notes,
            **preview.ledger_fields(),
        )
        # The dates are held here instead of as items on the change: the guardrails treat
        # every item as a price move, and a date-valued item would fail the delta check.
        self.promotion_windows[change.change_id] = {
            "name": promotion.name,
            "starts": promotion.starts,
            "ends": promotion.ends,
        }
        return change

    async def stage_campaign(
        self, session: MerchantSessionContext, campaign: CampaignDraft
    ) -> StagedChange:
        raise ChangeNotApplicable(CAMPAIGNS_READ_ONLY)

    # -- Change lifecycle -------------------------------------------------------------------

    async def get_pending_changes(self, session: MerchantSessionContext) -> list[StagedChange]:
        return self.ledger.pending()

    async def apply_change(self, session: MerchantSessionContext, change_id: str) -> StagedChange:
        """Push the change to WooCommerce, then mark it applied. Guardrails run again here
        because the config may have changed since staging. The ledger is updated only after
        the write succeeds, so a rejected write leaves the change in ``staged`` for another
        attempt."""
        change = self.ledger.get(change_id)
        if change is None:
            raise ChangeNotApplicable(f"unknown change id {change_id!r} in the ledger")
        if change.status is not ChangeStatus.STAGED:
            raise ChangeNotApplicable(
                f"change {change_id} has status {change.status.value}; only staged changes can "
                "be applied"
            )
        violations = check_guardrails(change.kind, change.items, self._config)
        if violations:
            raise GuardrailViolation(violations)
        try:
            notes = await self._writer.apply(
                change, promotion_window=self.promotion_windows.get(change_id)
            )
        except WriteFailed as error:
            if error.completed or error.uncertain:
                # Part of the change is on the store, so the staged ``before`` values no
                # longer describe it and the rest cannot be replayed against them: every
                # written target would now be refused for having moved. Discarding forces a
                # fresh reading of the store rather than leaving a change that can only fail.
                self.ledger.discard(change_id, session.operator)
                raise ChangeNotApplicable(
                    f"WooCommerce rejected change {change_id}: {error}. The change was "
                    "discarded rather than left approvable, because part of the store was "
                    "already written."
                ) from error
            raise ChangeNotApplicable(
                f"WooCommerce rejected change {change_id}: {error}. The change is still staged."
            ) from error
        self.orders.invalidate()
        applied = self.ledger.apply(change_id, session.operator)
        if notes:
            applied.guardrail_notes = [*applied.guardrail_notes, *notes]
        return applied

    async def discard_change(
        self,
        session: MerchantSessionContext,
        change_id: str,
        actor_kind: ActorKind = ActorKind.OPERATOR,
    ) -> StagedChange:
        return self.ledger.discard(change_id, session.operator, actor_kind)

    # -- Merchant context ---------------------------------------------------------------------

    def _limitations(self) -> list[DataLimitation]:
        window = self.orders.window_days
        return [
            DataLimitation(source="analytics", note=TRAFFIC_NOTE),
            DataLimitation(
                source="orders",
                note=f"orders are scanned {window} days back; periods before that return no data",
            ),
            DataLimitation(source="campaigns", note=CAMPAIGN_FIGURES),
        ]

    async def get_merchant_context(self, session: MerchantSessionContext) -> dict[str, Any] | None:
        """Store facts injected into every model request. Beyond identity and currency, it
        names the metric source and lists what the store cannot report, so the model can
        distinguish "not measured" from "zero"."""
        profile = await self.profile()
        metrics = await self.metrics()
        catalog_size = len(await self.catalog.all())
        return {
            "store": profile.name,
            "store_url": profile.url,
            "currency": profile.currency,
            "timezone": profile.timezone,
            "woocommerce_version": profile.woocommerce_version,
            "catalog_size": catalog_size,
            "default_period": resolve_period(None, default_days=7).label,
            "order_history_days": self.orders.window_days,
            "data_source": metrics.source_note,
            "pending_changes": len(self.ledger.pending()),
            "limitations": [entry.model_dump() for entry in self._limitations()],
        }

    # -- Portal extras -------------------------------------------------------------------------

    def kpi_trends(self) -> dict[str, list[dict[str, Any]]]:
        """Thirty daily points each for sales, orders, and average order value, computed
        from the cached order scan for the portal's KPI sparklines."""
        last_full_day = datetime.now(UTC).date() - timedelta(days=1)
        first_day = last_full_day - timedelta(days=TREND_DAYS - 1)
        rows = daily_rows(self.orders.cached(), first_day, last_full_day)
        return {
            name: [{"date": row.day.isoformat(), "value": value_of(row)} for row in rows]
            for name, value_of in _TREND_VALUES.items()
        }

    def home_insights(self, limit: int = 3) -> list[dict[str, Any]]:
        """Suggestion cards for the portal overview, each carrying a prefilled prompt. They
        are derived from the same alert functions the agent's tools call, so the cards and
        the conversation agree about what needs attention."""
        pools: dict[str, Sequence[Any]] = {
            "inventory": self.alerts.inventory_alerts_cached(),
            "issues": self.alerts.order_issues_cached(),
            "pending": self.ledger.pending(),
        }
        cards: list[dict[str, Any]] = []
        for card in _INSIGHT_CARDS:
            hits = card.pick(pools)
            if hits:
                cards.append(card.render(hits))
        return cards[:limit]

    async def warm(self) -> None:
        """Prime the profile, catalog, and order caches so the portal's first render is
        served from memory."""
        await self.profile()
        await self.catalog.all()
        await self.orders.orders()


def _day_of(stamp: str | None) -> str | None:
    """The ``YYYY-MM-DD`` part of a WooCommerce timestamp, or None."""
    return (stamp or "")[:10] or None


def _amount(money: Any) -> float | None:
    """A ``{"value", "currency", "formatted"}`` price block as a float, or None when the
    channel supplied none. Absent is absent: never a stand-in zero."""
    if not isinstance(money, dict):
        return None
    try:
        return round(float(money.get("value")), 2)
    except (TypeError, ValueError):
        return None


def campaign_from_node(node: dict[str, Any], currency: str) -> Campaign:
    """One ``wc-admin/marketing/campaigns`` row as the interface's ``Campaign``. Core exposes
    an id, the channel's slug, a title, a manage URL, and, where the channel reports them,
    ``cost`` and ``sales``. It has no budget, status, or dates. Spend and revenue are None
    when absent; budget is required by the interface and has no source, so it reads 0 and
    the merchant context says why; status is ``active`` because a channel lists only the
    campaigns it currently runs."""
    cost = node.get("cost") if isinstance(node.get("cost"), dict) else None
    sales = node.get("sales") if isinstance(node.get("sales"), dict) else None
    return Campaign(
        campaign_id=str(node.get("id")),
        name=str(node.get("title") or node.get("id")),
        status="active",
        channel=str(node.get("channel") or "") or None,
        budget=0.0,
        spend=_amount(cost),
        revenue=_amount(sales),
        currency=str((cost or sales or {}).get("currency") or currency),
    )
