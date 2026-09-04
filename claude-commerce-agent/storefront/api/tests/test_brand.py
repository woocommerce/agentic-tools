# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""``BrandSource``: the root index's fields, the bridge's additions, the contrast guard on
the colour pair, and what happens when the site is down."""

from __future__ import annotations

import httpx
import pytest

from storefront.api.brand import FALLBACK_TAGLINE, BrandSource, contrast_ratio, readable_pair
from storefront.api.store_client import StoreApiClient

from .fake_store import BRAND_COLOURS, STORE_URL, TAGLINE, FakeWooSite


def source_over(site: FakeWooSite) -> BrandSource:
    return BrandSource(StoreApiClient(STORE_URL, http=site.client()))


async def test_the_bridge_adds_the_logo_and_colours(site: FakeWooSite):
    payload = await source_over(site).brand()
    assert payload["name"] == "ACME Supply Co."
    assert payload["tagline"] == payload["slogan"] == TAGLINE
    assert payload["logo_url"] == f"{STORE_URL}/logo.png"
    assert payload["colors"] == BRAND_COLOURS


async def test_the_root_index_alone_gives_the_name_and_site_icon():
    payload = await source_over(FakeWooSite(bridge=False)).brand()
    assert payload["name"] == "ACME Supply Co."
    assert payload["logo_url"] == f"{STORE_URL}/icon.png"
    assert "colors" not in payload


async def test_a_good_read_is_remembered(site: FakeWooSite):
    source = source_over(site)
    await source.brand()
    await source.brand()
    assert len(site.sent("")) == 1
    source.forget()
    await source.brand()
    assert len(site.sent("")) == 2


async def test_a_site_that_is_down_gets_fallbacks_that_are_not_remembered(monkeypatch):
    monkeypatch.delenv("BRAND_TAGLINE", raising=False)
    hits = 0

    def refuse(request: httpx.Request) -> httpx.Response:
        nonlocal hits
        hits += 1
        return httpx.Response(503)

    client = StoreApiClient(
        STORE_URL, http=httpx.AsyncClient(transport=httpx.MockTransport(refuse)), retry_backoff=0
    )
    source = BrandSource(client)
    for _ in range(2):
        payload = await source.brand()
        assert payload["name"] == STORE_URL
        assert payload["tagline"] == FALLBACK_TAGLINE
        assert "colors" not in payload
    assert hits >= 2  # the second call went back to the site


@pytest.mark.parametrize(
    ("background", "foreground", "readable"),
    [
        ("#ffffff", "#000000", True),  # 21:1
        ("#1f2a44", "#fff", True),  # short form accepted
        ("#ffffff", "#ffffff", False),  # the white-on-white theme
        ("#888888", "#999999", False),  # present, far below 3:1
        ("#ffffff", None, False),
        ("var(--wp--preset--color--base)", "#000000", False),  # not a hex colour
        ("#12345", "#000000", False),  # wrong length
    ],
)
def test_the_contrast_guard(background, foreground, readable):
    assert readable_pair(background, foreground) is readable


def test_contrast_ratio_matches_the_wcag_reference_values():
    assert contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast_ratio("#ffffff", "#ffffff") == pytest.approx(1.0)
    assert contrast_ratio("#767676", "#ffffff") == pytest.approx(4.54, abs=0.01)
    assert contrast_ratio("nope", "#ffffff") is None
