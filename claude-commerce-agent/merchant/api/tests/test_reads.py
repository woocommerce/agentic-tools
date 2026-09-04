# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""Read paths of ``WooMerchantBackend`` over the seeded local store. Beyond the mapping of
products and orders, these pin the backend's honesty: where WooCommerce records no figure
the answer is None, never a zero that looks measured."""

from __future__ import annotations

import pytest
from merchant_agent import ListingFilters
from merchant_agent.changes import ChangeNotApplicable

from merchant.api.store_view import WooStoreView

from .conftest import (
    APRON,
    BENCH_DOGS,
    BROOM,
    CUSHION,
    HOLD_DOWN,
    MALLET,
    SQUARE,
    STOOL,
    STOOL_THREE_STEP,
    STOOL_TWO_STEP,
    STORE_TITLE,
)


def ids(listings) -> list[str]:
    return [entry.listing_id for entry in listings]


async def details_of(backend, session, listing_id: str):
    found = await backend.get_listing(session, listing_id)
    assert found is not None, f"listing {listing_id} should resolve"
    return found


# -- Store and catalog ------------------------------------------------------------------


async def test_profile_comes_from_site_title_and_currency_setting(backend) -> None:
    profile = await backend.profile()
    assert (profile.name, profile.currency) == (STORE_TITLE, "USD")
    assert backend.store_name == STORE_TITLE


async def test_catalog_omits_the_trashed_product(backend) -> None:
    await backend.warm()
    catalog = set(ids(backend.all_listings()))
    assert MALLET not in catalog
    assert catalog == {APRON, STOOL, BENCH_DOGS, CUSHION, SQUARE, BROOM, HOLD_DOWN}


async def test_variable_product_reads_as_parent_with_variants(backend, session) -> None:
    stool = await details_of(backend, session, STOOL)
    assert stool.options == {"Size": ["Two-step", "Three-step"]}
    # The parent shows the lowest variation price and the summed stock.
    assert (stool.price, stool.stock) == (60.0, 23)
    assert [(v.listing_id, v.option_values, v.price, v.stock) for v in stool.variants] == [
        (STOOL_TWO_STEP, {"Size": "Two-step"}, 60.0, 14),
        (STOOL_THREE_STEP, {"Size": "Three-step"}, 90.0, 9),
    ]
    assert {v.variant_of for v in stool.variants} == {STOOL}


async def test_variation_resolves_by_its_own_id(backend, session) -> None:
    rung = await details_of(backend, session, STOOL_THREE_STEP)
    assert rung.listing_id == STOOL_THREE_STEP
    assert rung.variant_of == STOOL
    assert rung.title == "Folding step stool — Three-step"
    assert rung.sales_last_30d is not None


@pytest.mark.parametrize(
    ("listing_id", "status"), [(CUSHION, "paused"), (APRON, "active")], ids=["draft", "publish"]
)
async def test_post_status_maps_to_listing_status(backend, session, listing_id, status) -> None:
    assert (await details_of(backend, session, listing_id)).status == status


async def test_content_gaps_are_itemised(backend, session) -> None:
    thin = await details_of(backend, session, BENCH_DOGS)
    assert thin.content_quality == "poor"
    assert {"long_description", "short_description", "images"} <= set(thin.missing_attributes)
    full = await details_of(backend, session, APRON)
    assert full.content_quality == "good"


async def test_approved_reviews_become_snippets(backend, session) -> None:
    apron = await details_of(backend, session, APRON)
    assert len(apron.review_snippets) == 2
    assert apron.review_snippets[0].startswith("Pockets")


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("step stool", [STOOL]),
        ("ACME-BD-4", [BENCH_DOGS]),
        (APRON, [APRON]),
        ("quantum flux capacitor", []),
    ],
    ids=["title words", "sku", "numeric id", "no match"],
)
async def test_search_by_words_sku_and_id(backend, session, query, expected) -> None:
    hits = await backend.search_listings(session, query, None, 5)
    assert ids(hits)[: len(expected)] == expected
    if not expected:
        assert hits == []


async def test_search_filters_narrow_by_status_and_stock(backend, session) -> None:
    paused = await backend.search_listings(session, "all", ListingFilters(status="paused"), 10)
    assert ids(paused) == [CUSHION]
    shallow = await backend.search_listings(session, "all", ListingFilters(max_stock=5), 10)
    assert BENCH_DOGS in ids(shallow)


@pytest.mark.parametrize("listing_id", ["999", "no-such-slug"])
async def test_unknown_ids_read_as_absent(backend, session, listing_id) -> None:
    assert await backend.get_listing(session, listing_id) is None
    assert await backend.get_pricing_context(session, listing_id) is None


# -- Pricing ----------------------------------------------------------------------------


async def test_cost_of_goods_drives_margin_and_floor(backend, session) -> None:
    apron = await backend.get_pricing_context(session, APRON)
    assert apron is not None
    assert (apron.current_price, apron.unit_cost, apron.margin_pct) == (40.0, 10.0, 75.0)
    assert apron.max_price_delta_pct == 20.0
    assert (apron.min_price, apron.max_price) == (32.0, 48.0)
    assert apron.min_price_basis == "policy"


async def test_no_recorded_cost_means_no_margin(backend, session) -> None:
    square = await backend.get_pricing_context(session, SQUARE)
    assert square is not None
    # Unknown cost is reported as unknown, not as a hundred percent margin.
    assert square.unit_cost is None
    assert square.margin_pct is None


async def test_pricing_context_of_a_parent_lists_each_variation(backend, session) -> None:
    stool = await backend.get_pricing_context(session, STOOL)
    assert stool is not None
    assert stool.current_price == 60.0
    by_variation = {v.listing_id: (v.current_price, v.margin_pct) for v in stool.variants}
    assert by_variation == {STOOL_TWO_STEP: (60.0, 63.3), STOOL_THREE_STEP: (90.0, 65.6)}


# -- Metrics ----------------------------------------------------------------------------


async def test_snapshot_reports_traffic_as_unmeasured(backend, session) -> None:
    snapshot = await backend.get_business_snapshot(session, None)
    assert snapshot.sales > 0
    assert snapshot.orders > 0
    assert snapshot.traffic is None
    assert snapshot.conversion_rate is None
    assert "records no sessions" in (snapshot.note or "")
    # The generated history gives the snapshot a prior period to compare against.
    assert snapshot.compare_to
    assert snapshot.sales_change_pct is not None


async def test_analytics_and_order_scan_total_the_same_sales(
    backend, no_analytics, session
) -> None:
    via_analytics = await backend.query_metrics(session, "sales")
    via_orders = await no_analytics.query_metrics(session, "sales")
    assert len(via_analytics.points) == len(via_orders.points) == 30
    assert via_analytics.unit == "USD"
    total = lambda series: round(sum(point.value for point in series.points), 2)  # noqa: E731
    assert total(via_analytics) == total(via_orders)
    analytics_context = await backend.get_merchant_context(session)
    orders_context = await no_analytics.get_merchant_context(session)
    assert "WooCommerce Analytics" in analytics_context["data_source"]
    assert "order scan" in orders_context["data_source"]


async def test_traffic_series_is_empty_with_an_explanation(backend, session) -> None:
    traffic = await backend.query_metrics(session, "traffic")
    assert traffic.points == []
    assert "records no sessions" in (traffic.note or "")


async def test_category_segment_never_exceeds_the_whole(backend, session) -> None:
    storage = await backend.query_metrics(session, "orders", segment="storage", granularity="week")
    whole = await backend.query_metrics(session, "orders", granularity="week")
    assert sum(p.value for p in storage.points) <= sum(p.value for p in whole.points)


# -- Alerts, issues, and context --------------------------------------------------------


async def test_inventory_alerts_use_the_per_slug_threshold(backend, session) -> None:
    alerts = await backend.get_inventory_alerts(session)
    by_listing = {alert.listing_id: alert for alert in alerts}
    assert by_listing[BENCH_DOGS].kind == "low_stock"
    assert by_listing[BENCH_DOGS].threshold == 6  # thresholds.json names this slug
    assert by_listing[HOLD_DOWN].kind == "slow_mover"
    assert alerts[0].kind == "low_stock"  # low stock sorts ahead of slow movers


async def test_order_issues_cover_all_three_kinds(backend, session) -> None:
    issues = await backend.get_order_issues(session)
    by_kind = {issue.kind: issue for issue in issues}
    assert set(by_kind) == {"delayed", "buyer_message", "return_spike"}
    assert "neighbour" in (by_kind["buyer_message"].buyer_message_excerpt or "")
    assert by_kind["return_spike"].listing_id == BENCH_DOGS


async def test_campaign_read_refuses_with_the_reason(backend, session) -> None:
    with pytest.raises(ChangeNotApplicable, match="no campaign object"):
        await backend.get_campaign_performance(session, None)


async def test_merchant_context_states_its_limits(backend, session) -> None:
    context = await backend.get_merchant_context(session)
    assert context["store"] == STORE_TITLE
    assert context["order_history_days"] == 60
    assert {limit["source"] for limit in context["limitations"]} == {
        "analytics",
        "orders",
        "campaigns",
    }


async def test_store_view_serves_the_portal_from_the_caches(backend) -> None:
    await backend.warm()
    view = WooStoreView(backend)
    assert view.store_name == STORE_TITLE
    assert len(view.products) == 7
    recent = view.recent_orders(6)
    assert len(recent) == 6
    assert recent[0].placed_at >= recent[-1].placed_at
