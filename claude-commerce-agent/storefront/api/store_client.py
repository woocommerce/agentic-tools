# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""Thin HTTP layer over a WooCommerce site's public REST namespaces.

Everything the storefront needs is reachable without a credential: the Store API
(``wc/store/v1``) for products, the cart, and shipping rates; the core WordPress API
(``wp/v2``) for pages; the root index for the site name and the namespaces it advertises;
and the bridge plugin (``claude-commerce/v1``) when a site runs it. The one piece of state
that travels with a request is the ``Cart-Token`` header WooCommerce hands out on the
first cart read.

Failures surface as :class:`StoreApiError` carrying WordPress's error slug and the HTTP
status. A token WooCommerce no longer honours is the narrower :class:`CartTokenError`, so
the backend can start a fresh cart instead of failing the shopper. A read gets one retry
after ``retry_backoff`` seconds on rate limiting (429) or a server error (5xx); a write is
retried only on 429. Tests inject an ``httpx.AsyncClient`` over an ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

STORE_V1 = "wc/store/v1"
WP_V2 = "wp/v2"
BRIDGE_V1 = "claude-commerce/v1"
CART_TOKEN_HEADER = "Cart-Token"

DEFAULT_STORE_URL = "http://localhost:8090"
REQUEST_TIMEOUT = 20.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# A write that returned a server error may still have landed: a 502 from a proxy says
# nothing about what the store did. The cart routes add to a line's quantity rather than
# set it, so repeating one can double it. 429 is the exception, because the store refused
# the request before touching the cart.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
WRITE_RETRY_STATUSES = frozenset({429})
REJECTED_TOKEN_SLUGS = frozenset(
    {"woocommerce_rest_invalid_cart_token", "woocommerce_rest_cart_token_expired"}
)

JsonAndHeaders = tuple[Any, dict[str, str]]


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return url.scheme, url.host, url.port


def normalize_store_url(raw: str) -> str:
    """Trim whitespace and trailing slashes; assume https when no scheme was given."""
    url = raw.strip().rstrip("/")
    if url and "://" not in url:
        url = "https://" + url
    return url


def store_url_from_env(default: str = DEFAULT_STORE_URL) -> str:
    return normalize_store_url(os.environ.get("WOOCOMMERCE_STORE_URL") or default)


class StoreApiError(RuntimeError):
    """A non-2xx answer from the site. ``code`` is WordPress's error slug (empty when the
    body carried none) and ``status`` the HTTP status."""

    def __init__(self, message: str, *, code: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status = status

    @property
    def not_found(self) -> bool:
        return self.status == 404 or "invalid" in self.code or "not_found" in self.code


class CartTokenError(StoreApiError):
    """WooCommerce rejected the Cart-Token: expired, tampered, or from another site."""


def cart_token_of(headers: dict[str, str]) -> str | None:
    """The ``Cart-Token`` response header, whatever its casing, or None."""
    wanted = CART_TOKEN_HEADER.lower()
    return next((value for key, value in headers.items() if key.lower() == wanted and value), None)


class StoreApiClient:
    """One site, one ``httpx`` client. Paths are given relative to ``/wp-json/``."""

    def __init__(
        self,
        store_url: str,
        *,
        retry_backoff: float = 0.5,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.store_url = normalize_store_url(store_url)
        self.retry_backoff = retry_backoff
        self.http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT), follow_redirects=True
        )

    def url(self, path: str) -> str:
        return f"{self.store_url}/wp-json/{path.lstrip('/')}"

    async def aclose(self) -> None:
        await self.http.aclose()

    async def get(
        self, path: str, params: dict[str, Any] | None = None, *, cart_token: str | None = None
    ) -> JsonAndHeaders:
        return await self.request("GET", path, params=params, cart_token=cart_token)

    async def post(
        self, path: str, json: Any = None, *, cart_token: str | None = None
    ) -> JsonAndHeaders:
        return await self.request("POST", path, json=json, cart_token=cart_token)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        cart_token: str | None = None,
    ) -> JsonAndHeaders:
        """Send one request, retrying once on a transient status it is safe to repeat for
        this method, and decode the answer."""
        headers = {"Accept": "application/json"}
        if cart_token:
            headers[CART_TOKEN_HEADER] = cart_token
        query = {key: value for key, value in (params or {}).items() if value is not None}
        retryable = RETRY_STATUSES if method.upper() in SAFE_METHODS else WRITE_RETRY_STATUSES
        attempts = 0
        while True:
            attempts += 1
            response = await self._request_following_safe_redirects(
                method, self.url(path), params=query, json=json, headers=headers
            )
            if response.status_code not in retryable or attempts > 1:
                break
            await asyncio.sleep(self.retry_backoff)
        return self._unpack(response)

    async def _request_following_safe_redirects(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any],
        json: Any,
        headers: dict[str, str],
    ) -> httpx.Response:
        """Follow redirects while keeping a cart capability on the store's origin only."""
        origin = _origin(httpx.URL(url))
        response = await self.http.request(
            method, url, params=params, json=json, headers=headers, follow_redirects=False
        )
        redirects = 0
        while response.has_redirect_location:
            if redirects >= self.http.max_redirects:
                raise httpx.TooManyRedirects(
                    "Exceeded maximum allowed redirects.", request=response.request
                )
            next_request = response.next_request
            if next_request is None:
                break
            if _origin(next_request.url) != origin:
                next_request.headers.pop(CART_TOKEN_HEADER, None)
            response = await self.http.send(next_request, follow_redirects=False)
            redirects += 1
        return response

    @staticmethod
    def _unpack(response: httpx.Response) -> JsonAndHeaders:
        body: Any = None
        if response.content:
            try:
                body = response.json()
            except ValueError:
                body = None
        if response.is_success:
            return body, dict(response.headers)
        slug, text = "", ""
        if isinstance(body, dict):
            slug = str(body.get("code") or "")
            text = str(body.get("message") or "")
        text = text or f"HTTP {response.status_code}"
        kind = CartTokenError if slug in REJECTED_TOKEN_SLUGS else StoreApiError
        raise kind(text, code=slug, status=response.status_code)
