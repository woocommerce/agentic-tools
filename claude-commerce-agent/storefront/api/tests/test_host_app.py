# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""The wired FastAPI app over the fake site: what the startup probe learns, the grid
pre-fill, cart payload extras, the attach route, the direct-add gate, and branding."""

from __future__ import annotations

import pytest
from demo_common import SESSION_HEADER
from fastapi.testclient import TestClient

from storefront.api import main as main_module

from .conftest import Seed
from .fake_store import BRAND_COLOURS, STORE_URL, TAGLINE, FakeWooSite

service = main_module.service


@pytest.fixture
def site() -> FakeWooSite:
    return FakeWooSite()


@pytest.fixture
def web(site: FakeWooSite, monkeypatch):
    """The module's own service, pointed at the fake before its lifespan runs. Entering
    the TestClient runs the probe and the grid pre-fill."""
    monkeypatch.setattr(service.client, "http", site.client())
    service.backend.bridge_available = None
    service.backend.catalog.clear()
    service.brand.forget()
    with TestClient(service.app, base_url="http://localhost") as web:
        yield web


class Shopper:
    """A session on the app, with the helpers a test needs to drive it."""

    def __init__(self, web: TestClient) -> None:
        self.web = web
        started = web.post("/api/session", json={"user_id": "guest"})
        self.headers = {SESSION_HEADER: started.json()["session_id"]}

    def grant_provenance(self, *product_ids: str) -> None:
        """What a search tool call would have done for these ids."""
        record = service.host.sessions.require(self.headers[SESSION_HEADER])
        record.state.remember_products([service.backend.products[pid] for pid in product_ids])
        service.host.sessions.save(record)

    def add(self, product_id: str, quantity: int = 1):
        body = {"product_id": product_id, "quantity": quantity}
        return self.web.post("/api/cart/add", json=body, headers=self.headers)

    def attach(self, token: str):
        return self.web.post("/api/cart/attach", json={"cart_token": token}, headers=self.headers)

    def cart(self) -> dict:
        return self.web.get("/api/cart", headers=self.headers).json()


def test_the_probe_names_the_store_and_finds_the_bridge(web):
    health = web.get("/api/health").json()
    assert (health["ok"], health["store"]) == (True, "ACME Supply Co.")
    assert service.backend.bridge_available is True
    config = service.host.agent.config
    assert config.brand_name == "ACME Supply Co."
    assert "placed from this conversation's cart" in config.domain_search_notes


def test_the_grid_is_filled_before_any_session_exists(web):
    products = web.get("/api/products").json()["products"]
    assert {p["product_id"] for p in products} >= {Seed.APRON, Seed.STOOL}
    stool = web.get(f"/api/products/{Seed.STOOL}").json()
    assert stool["options"] == {"Size": ["Two-step", "Three-step"]}
    assert web.get(f"/api/products/{Seed.UNKNOWN}").status_code == 404


def test_a_fresh_session_has_no_cart_token_and_no_handoff(web):
    payload = Shopper(web).cart()
    assert payload["items"] == []
    assert (payload["checkout_url"], payload["cart_token"]) == (None, None)


def test_the_add_button_needs_provenance_then_returns_the_handoff(web, site: FakeWooSite):
    shopper = Shopper(web)
    held = shopper.add(Seed.APRON)
    assert held.status_code == 400
    assert "not in this session" in held.json()["detail"]

    shopper.grant_provenance(Seed.APRON)
    added = shopper.add(Seed.APRON).json()
    assert added["ok"] is True
    assert added["cart"]["item_count"] == 1
    assert added["cart_token"] in site.carts
    assert "claude_commerce_cart=" in added["checkout_url"]
    assert shopper.cart()["checkout_url"] == added["checkout_url"]


def test_attach_continues_in_another_session_s_cart(web):
    first = Shopper(web)
    first.grant_provenance(Seed.APRON)
    token = first.add(Seed.APRON).json()["cart_token"]

    second = Shopper(web)
    attached = second.attach(token)
    joined = attached.json()
    assert (attached.status_code, joined["item_count"], joined["cart_token"]) == (200, 1, token)
    assert second.attach("tok-unknown").status_code == 404


def test_the_brand_route_serves_the_site_s_branding(web):
    payload = web.get("/api/brand").json()
    assert payload["name"] == "ACME Supply Co."
    assert payload["tagline"] == TAGLINE
    assert payload["logo_url"] == f"{STORE_URL}/logo.png"
    assert payload["colors"] == BRAND_COLOURS
