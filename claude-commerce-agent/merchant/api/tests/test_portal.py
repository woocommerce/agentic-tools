# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""The HTTP portal: the reference's merchant router mounted over ``WooMerchantBackend``.
The apply and discard routes get the closest look, because they are what the operator
presses to make a staged change real or to drop it."""

from __future__ import annotations

import pytest
from demo_common import SESSION_HEADER
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from merchant_agent import ChangeStatus, PriceUpdateItem

from merchant.api.main import create_app

from .conftest import (
    APRON,
    BENCH_DOGS,
    SITE_HOST,
    SITE_URL,
    STOOL,
    STOOL_TWO_STEP,
    STORE_TITLE,
    site_settings,
)

CREDENTIAL_VARS = (
    "WOOCOMMERCE_STORE_URL",
    "WOOCOMMERCE_CONSUMER_KEY",
    "WOOCOMMERCE_CONSUMER_SECRET",
)


@pytest.fixture
def portal(portal_factory, store, settings):
    return portal_factory(settings, executor=store)


@pytest.fixture
async def warm_portal(portal):
    """The overview reads caches the lifespan hook would have filled at startup."""
    await portal.backend.warm()
    return portal


async def stage_apron_price(portal, session, price: float = 44.0):
    return await portal.backend.stage_price_update(
        session, [PriceUpdateItem(listing_id=APRON, new_price=price)]
    )


# -- Reads ------------------------------------------------------------------------------


def test_health_names_role_skills_and_site(portal) -> None:
    health = portal.read("/health")
    assert health["ok"] is True
    assert health["role"] == "merchant"
    assert health["skills"]
    assert health["store"] == SITE_HOST  # the site title is only known after warm()


def test_overview_needs_a_session(portal) -> None:
    assert portal.http.get("/api/merchant/overview").status_code == 401


async def test_overview_carries_every_dashboard_section(warm_portal) -> None:
    overview = warm_portal.read("/overview")
    assert overview["snapshot"]["currency"] == "USD"
    assert overview["snapshot"].get("traffic") is None
    assert overview["store_url"] == SITE_URL
    assert overview["store_kind"] == "woocommerce"
    assert set(overview["trends"]) == {"sales", "orders", "average_order_value"}
    assert {len(series) for series in overview["trends"].values()} == {30}
    assert overview["insights"]
    assert all(card["insight_id"] and card["prompt"] for card in overview["insights"])
    attention = overview["needs_attention"]
    assert attention["inventory"][0]["kind"] == "low_stock"
    assert {issue["kind"] for issue in attention["order_issues"]} == {
        "delayed",
        "buyer_message",
        "return_spike",
    }
    assert len(overview["recent_orders"]) == 6


async def test_overview_labels_the_local_store(portal_factory, store, settings) -> None:
    local = portal_factory(site_settings(local_store=True), executor=store)
    await local.backend.warm()
    assert local.read("/overview")["store_kind"] == "local"


async def test_listings_route_lists_and_searches(warm_portal) -> None:
    assert warm_portal.read("/listings")["total"] == 7
    found = warm_portal.read("/listings", query="apron")["listings"]
    assert [entry["listing_id"] for entry in found] == [APRON]


def test_listing_detail_includes_pricing_and_404s_unknown_ids(portal) -> None:
    apron = portal.read(f"/listings/{APRON}")
    assert apron["listing"]["title"] == "Canvas tool apron"
    assert apron["pricing"]["current_price"] == 40.0
    stool = portal.read(f"/listings/{STOOL}")
    assert len(stool["pricing"]["variants"]) == 2
    assert portal.get("/listings/999").status_code == 404


def test_alerts_route_agrees_with_the_backend(portal) -> None:
    alerts = portal.read("/alerts")
    low = [a["listing_id"] for a in alerts["inventory"] if a["kind"] == "low_stock"]
    assert low == [BENCH_DOGS]
    assert len(alerts["order_issues"]) == 3


# -- Approve and discard ----------------------------------------------------------------


async def test_approve_button_writes_once_and_drops_the_mark(portal, session, store) -> None:
    change = await stage_apron_price(portal, session)
    change_id = portal.give_provenance(change)
    assert store.applied == []

    outcome = portal.apply(change_id).json()

    assert outcome["ok"] is True
    assert outcome["change"]["status"] == "applied"
    assert [(verb, path) for verb, path, _ in store.applied] == [("PUT", f"products/{APRON}")]
    record = portal.record()
    assert record.pending_app_events == [
        f"Operator approved and applied change {change_id} from the preview card."
    ]
    assert record.state.approved_change_ids == set()


async def test_apply_holds_a_change_the_session_never_saw(portal, session, store) -> None:
    change = await stage_apron_price(portal, session)  # no provenance given
    outcome = portal.apply(change.change_id).json()
    assert outcome["ok"] is False
    assert outcome["change"] is None
    assert store.applied == []
    assert portal.backend.ledger.get(change.change_id).status is ChangeStatus.STAGED


def test_apply_of_an_unknown_id_is_a_hold_not_an_error(portal) -> None:
    response = portal.apply("chg-none")
    assert response.status_code == 200
    assert response.json()["ok"] is False


async def test_refused_write_is_400_and_keeps_the_change_staged(portal, session, store) -> None:
    change = await stage_apron_price(portal, session)
    change_id = portal.give_provenance(change)
    store.products = [row for row in store.products if row["id"] != int(APRON)]
    assert portal.apply(change_id).status_code == 400
    assert portal.backend.ledger.get(change_id).status is ChangeStatus.STAGED


async def test_discard_button_names_the_operator_as_actor(portal, session, store) -> None:
    change = await portal.backend.stage_price_update(
        session, [PriceUpdateItem(listing_id=STOOL_TWO_STEP, new_price=66.0)]
    )
    outcome = portal.discard(portal.give_provenance(change)).json()
    assert outcome["ok"] is True
    assert outcome["change"]["discarded_by_kind"] == "operator"
    assert store.applied == []


# -- create_app() with and without settings --------------------------------------------


def test_app_without_credentials_serves_only_health(monkeypatch) -> None:
    for name in (*CREDENTIAL_VARS, "WOOCOMMERCE_LOCAL_STORE"):
        monkeypatch.delenv(name, raising=False)
    app = create_app()
    mounted = [route.path for route in app.routes if isinstance(route, APIRoute)]
    assert mounted == ["/api/merchant/health"]
    with TestClient(app, base_url="http://localhost") as http:
        health = http.get("/api/merchant/health").json()
    assert health["ok"] is False
    for expected in (*CREDENTIAL_VARS, ".env"):
        assert expected in health["error"]


def test_local_store_flag_alone_serves_a_full_portal(monkeypatch) -> None:
    monkeypatch.setenv("WOOCOMMERCE_LOCAL_STORE", "1")
    for name in CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_app(), base_url="http://localhost") as http:
        health = http.get("/api/merchant/health").json()
        started = http.post("/api/merchant/session").json()
        overview = http.get(
            "/api/merchant/overview", headers={SESSION_HEADER: started["session_id"]}
        ).json()
    assert health["ok"] is True
    assert health["store"] == STORE_TITLE
    assert overview["store_kind"] == "local"
    assert overview["snapshot"]["orders"] > 0
