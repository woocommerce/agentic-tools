# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""``LocalStore`` on its own, the mode ``WOOCOMMERCE_LOCAL_STORE=1`` selects. It has to
answer each REST read the backend issues, accept the writes an approved change produces,
and show them on the next read, so that stage, approve, re-read through the portal ends
with a visibly changed catalog."""

from __future__ import annotations

import pytest
from merchant_agent import MerchantSessionContext, PriceUpdateItem

from merchant.api.agent_config import LOCAL_STORE_URL
from merchant.api.local_store import LocalStore
from merchant.api.rest_client import WooApiError

from .conftest import APRON, MALLET, STOOL, STOOL_THREE_STEP, site_settings

APRON_TITLE = "Canvas tool apron"


def product_row(store: LocalStore, product_id: str) -> dict:
    return next(row for row in store.products if row["id"] == int(product_id))


async def put_product(store: LocalStore, product_id: str, **fields):
    node, _ = await store.request("PUT", f"wc/v3/products/{product_id}", json=fields)
    return node


# -- Loading ----------------------------------------------------------------------------


def test_seed_becomes_products_variations_and_a_history_of_orders(store) -> None:
    assert store.products[0]["slug"] == "canvas-tool-apron"
    # The trashed product is kept in the store; the catalog read is what hides it.
    assert product_row(store, MALLET)["status"] == "trash"
    assert [row["price"] for row in store.variations[int(STOOL)]] == ["60.00", "90.00"]
    assert len(store.orders) > 40  # generated history behind the handful of seed orders


# -- Reads the backend relies on --------------------------------------------------------


async def test_product_list_paginates_with_wp_headers(store) -> None:
    page, headers = await store.request(
        "GET", "wc/v3/products", params={"status": "any", "per_page": 5}
    )
    assert len(page) == 5
    assert headers["X-WP-TotalPages"] == "2"


async def test_variation_is_readable_by_post_id(store) -> None:
    row, _ = await store.request("GET", f"wc/v3/products/{STOOL_THREE_STEP}")
    assert row["type"] == "variation"
    assert row["parent_id"] == int(STOOL)


async def test_revenue_stats_report_totals_and_intervals(store) -> None:
    window = {"after": "2026-01-01T00:00:00", "before": "2030-01-01T00:00:00", "interval": "day"}
    report, _ = await store.request("GET", "wc-analytics/reports/revenue/stats", params=window)
    assert report["totals"]["orders_count"] > 0
    assert report["intervals"]


async def test_unknown_routes_and_fields_are_refused(store) -> None:
    with pytest.raises(WooApiError, match="no answer"):
        await store.request("GET", "wc/v3/coupons")
    with pytest.raises(WooApiError):
        await put_product(store, APRON, not_a_field=1)


async def test_sale_price_waits_for_its_start_date(store) -> None:
    scheduled = await put_product(
        store, APRON, sale_price="30.00", date_on_sale_from="2099-01-01T00:00:00"
    )
    assert scheduled["on_sale"] is False
    assert scheduled["price"] == "40.00"
    live = await put_product(store, APRON, date_on_sale_from=None)
    assert live["on_sale"] is True
    assert live["price"] == "30.00"


# -- The whole loop through the portal ---------------------------------------------------


@pytest.fixture
def local_portal(portal_factory):
    """A portal built the way ``WOOCOMMERCE_LOCAL_STORE=1`` builds it: local settings, no
    executor passed, so ``create_merchant_portal`` makes the LocalStore itself."""
    return portal_factory(
        site_settings(
            local_store=True, consumer_key="", consumer_secret="", store_url=LOCAL_STORE_URL
        )
    )


async def test_price_change_round_trips_through_the_portal(local_portal) -> None:
    await local_portal.backend.warm()
    store = local_portal.backend._executor  # the LocalStore the portal built for itself
    assert isinstance(store, LocalStore)

    def apron_price() -> float:
        listings = local_portal.read("/listings")["listings"]
        return next(entry["price"] for entry in listings if entry["title"] == APRON_TITLE)

    assert apron_price() == 40.0
    operator = MerchantSessionContext(
        session_id=local_portal.session_id, merchant_id="local", operator="Operator"
    )
    change = await local_portal.backend.stage_price_update(
        operator, [PriceUpdateItem(listing_id=APRON, new_price=44.0)]
    )
    assert product_row(store, APRON)["regular_price"] == "40.00"  # staged, not written

    response = local_portal.apply(local_portal.give_provenance(change))
    assert response.status_code == 200, response.text
    assert response.json()["change"]["status"] == "applied"

    assert product_row(store, APRON)["regular_price"] == "44.00"
    assert apron_price() == 44.0
