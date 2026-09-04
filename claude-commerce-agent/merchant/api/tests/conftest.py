# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""Fixtures shared by the merchant tests.

Everything runs over ``LocalStore``, an in-process stand-in for one WooCommerce site built
from ``data/seed.json``. The backend, caches, alert rules, staging writer, and portal
router are the production objects; only the transport is replaced. No socket is opened
and no credential is read. ``LocalStore.applied`` records every write it receives, which
is how the write-safety tests show that staging sends nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import demo_common.merchant as merchant_routes
import pytest
from commerce_common.memory import InMemoryMemoryStore
from demo_common import SESSION_HEADER
from demo_common.sessions import SessionRecord, SessionStore
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from merchant_agent import MerchantSessionContext, StagedChange

from merchant.api.agent_config import DATA_DIR, WooSettings, build_merchant_config
from merchant.api.local_store import LocalStore
from merchant.api.merchant import MerchantPortal, create_merchant_portal
from merchant.api.woo_backend import WooMerchantBackend

SEED_FILE = DATA_DIR / "seed.json"
SITE_URL = "https://acme-supply.example"
SITE_HOST = "acme-supply.example"
STORE_TITLE = "ACME Supply Co."
OPERATOR = "Dana"
SESSION_ID = "woo-test-session"
API = "/api/merchant"

# Post ids LocalStore gives the seed catalog: products count up from 101 in file order,
# and a variation is its parent id times ten plus its position.
APRON = "101"
STOOL = "102"
STOOL_TWO_STEP = "1021"
STOOL_THREE_STEP = "1022"
BENCH_DOGS = "103"
CUSHION = "104"
SQUARE = "105"
BROOM = "106"
HOLD_DOWN = "107"
MALLET = "108"


def site_settings(**overrides: Any) -> WooSettings:
    """Settings for a configured site served by LocalStore. Key and secret are placeholders
    that nothing above the transport layer inspects."""
    base = WooSettings(
        store_url=SITE_URL,
        consumer_key="ck_placeholder",
        consumer_secret="cs_placeholder",
        local_store=False,
        operator=OPERATOR,
        store_name=None,
        low_stock_default=8,
        fulfilment_sla_days=3,
        analytics_enabled=True,
    )
    return replace(base, **overrides) if overrides else base


@pytest.fixture
def store():
    return LocalStore.from_seed(SEED_FILE, store_url=SITE_URL)


@pytest.fixture
def settings():
    return site_settings()


@pytest.fixture
def config():
    return build_merchant_config(STORE_TITLE)


@pytest.fixture
def backend(store, settings, config):
    return WooMerchantBackend(store, settings, config)


@pytest.fixture
def no_analytics(store, config):
    """The same store with ``wc-analytics`` switched off, so metrics come from the order
    scan instead."""
    return WooMerchantBackend(store, site_settings(analytics_enabled=False), config)


@pytest.fixture
def session():
    return MerchantSessionContext(session_id=SESSION_ID, merchant_id=SITE_HOST, operator=OPERATOR)


# -- Portal harness ----------------------------------------------------------------------


@dataclass
class PortalHarness:
    """A mounted merchant portal and the handles a test needs around it: the HTTP client,
    the router's own session store, and the header of one started session."""

    portal: MerchantPortal
    sessions: SessionStore
    http: TestClient
    headers: dict[str, str]

    @property
    def backend(self) -> WooMerchantBackend:
        return self.portal.backend

    @property
    def session_id(self) -> str:
        return self.headers[SESSION_HEADER]

    def get(self, route: str, **params: Any) -> Response:
        return self.http.get(API + route, params=params or None, headers=self.headers)

    def read(self, route: str, **params: Any) -> Any:
        return self.get(route, **params).json()

    def post(self, route: str) -> Response:
        return self.http.post(API + route, headers=self.headers)

    def apply(self, change_id: str) -> Response:
        return self.post(f"/changes/{change_id}/apply")

    def discard(self, change_id: str) -> Response:
        return self.post(f"/changes/{change_id}/discard")

    def record(self) -> SessionRecord:
        return self.sessions.require(self.session_id)

    def give_provenance(self, change: StagedChange) -> str:
        """Mark ``change`` as seen by the HTTP session, as the assistant's own staging
        would. The apply and discard routes hold any id the session never met."""
        record = self.record()
        record.state.remember_change(change)
        self.sessions.save(record)
        return change.change_id


@pytest.fixture
def portal_factory(monkeypatch):
    """Build a :class:`PortalHarness`. The router constructs its ``SessionStore`` inside
    ``build_merchant_router`` and keeps it in a closure, so the class it instantiates is
    swapped for one that reports each instance back here."""
    built: list[SessionStore] = []

    class ReportingSessionStore(SessionStore):
        def __init__(self, state_type: type) -> None:
            super().__init__(state_type)
            built.append(self)

    monkeypatch.setattr(merchant_routes, "SessionStore", ReportingSessionStore)

    def build(settings: WooSettings, executor: Any = None) -> PortalHarness:
        portal = create_merchant_portal(settings, InMemoryMemoryStore(), executor=executor)
        app = FastAPI()
        app.include_router(portal.router, prefix=API)
        http = TestClient(app, base_url="http://localhost")
        started = http.post(API + "/session").json()
        return PortalHarness(portal, built[-1], http, {SESSION_HEADER: started["session_id"]})

    return build
