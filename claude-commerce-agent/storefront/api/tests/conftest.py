# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""Fixtures for the storefront suite. Every test talks to :class:`FakeWooSite`, an
in-process Store API over the shared seed catalog, so nothing leaves the process."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from shopping_agent import ShoppingSessionContext, ShoppingSessionState

from storefront.api.store_client import StoreApiClient
from storefront.api.woo_backend import WooStorefrontBackend

from .fake_store import STORE_URL, FakeWooSite


class Seed:
    """Post ids the local store assigns to the seed catalog (variations are ``parent * 10 + n``)."""

    APRON = "101"
    STOOL = "102"
    STOOL_TWO_STEP = "1021"
    STOOL_THREE_STEP = "1022"
    BENCH_DOGS = "103"
    DRAFT = "104"
    BROOM = "106"
    UNKNOWN = "999"


BackendFactory = Callable[[FakeWooSite], Awaitable[WooStorefrontBackend]]


async def probed_backend(site: FakeWooSite) -> WooStorefrontBackend:
    backend = WooStorefrontBackend(StoreApiClient(STORE_URL, http=site.client()))
    await backend.probe()
    return backend


@pytest.fixture
def site() -> FakeWooSite:
    return FakeWooSite()


@pytest.fixture
async def backend(site: FakeWooSite) -> WooStorefrontBackend:
    return await probed_backend(site)


@pytest.fixture
def make_backend() -> BackendFactory:
    """For tests that reconfigure the site (no bridge, say) before the backend probes it."""
    return probed_backend


@pytest.fixture
def shopper() -> ShoppingSessionContext:
    return ShoppingSessionContext(session_id="s-1", user_id="guest")


@pytest.fixture
def state() -> ShoppingSessionState:
    return ShoppingSessionState()
