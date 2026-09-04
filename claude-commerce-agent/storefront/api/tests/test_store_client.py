# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""``StoreApiClient``'s transport rules: which requests may be repeated, and where the
cart token is allowed to travel."""

from __future__ import annotations

import httpx
import pytest

from storefront.api.store_client import StoreApiClient, StoreApiError

from .fake_store import STORE_URL


def counting_client(status: int) -> tuple[StoreApiClient, list[str]]:
    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(status, json={"code": "boom", "message": "no"})

    client = StoreApiClient(
        STORE_URL, http=httpx.AsyncClient(transport=httpx.MockTransport(answer)), retry_backoff=0
    )
    return client, seen


async def test_a_read_is_retried_once_after_a_server_error() -> None:
    client, seen = counting_client(502)
    with pytest.raises(StoreApiError):
        await client.request("GET", "wc/store/v1/products")
    assert seen == ["GET", "GET"]


async def test_a_write_is_not_retried_after_a_server_error() -> None:
    """A 502 says nothing about whether the cart was changed, and add-item adds to the
    quantity rather than setting it, so repeating one can double a line."""
    client, seen = counting_client(502)
    with pytest.raises(StoreApiError):
        await client.request("POST", "wc/store/v1/cart/add-item", json={"id": 1, "quantity": 1})
    assert seen == ["POST"]


async def test_a_write_is_retried_after_a_rate_limit() -> None:
    """429 is refused before the store touches the cart, so repeating it is safe."""
    client, seen = counting_client(429)
    with pytest.raises(StoreApiError):
        await client.request("POST", "wc/store/v1/cart/add-item", json={"id": 1, "quantity": 1})
    assert seen == ["POST", "POST"]


@pytest.mark.parametrize(
    ("target", "expected_token"),
    [
        (f"{STORE_URL}/redirected", "cart-secret"),
        ("https://elsewhere.example/redirected", None),
    ],
    ids=["same origin keeps token", "different origin drops token"],
)
async def test_a_redirect_carries_the_cart_token_only_within_the_store_origin(
    target: str, expected_token: str | None
) -> None:
    received: list[str | None] = []

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cart"):
            return httpx.Response(302, headers={"Location": target})
        received.append(request.headers.get("Cart-Token"))
        return httpx.Response(200, json={})

    client = StoreApiClient(
        STORE_URL,
        http=httpx.AsyncClient(transport=httpx.MockTransport(answer), follow_redirects=True),
    )

    await client.get("wc/store/v1/cart", cart_token="cart-secret")

    assert received == [expected_token]
