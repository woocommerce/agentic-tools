# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""HTTP transport for the WooCommerce REST API.

``WooRestClient`` wraps one ``httpx.AsyncClient`` pointed at a site's ``/wp-json/`` root.
Each call carries the consumer key and secret as HTTP Basic credentials; a WordPress user
name plus an application password works identically, since WooCommerce accepts either
pair on that header.

Nothing above this module talks to ``httpx`` directly. ``WooMerchantBackend`` is typed
against the :class:`WooExecutor` protocol, which is what lets the test suite substitute a
scripted transport and lets ``local_store.py`` serve the same paths from memory, neither of
them subclassing anything defined here.

Error handling is concentrated here too. A WordPress error envelope (``code``,
``message``, ``data.status``) is raised as :class:`WooApiError`; when the status is 401 or
403 the subclass :class:`WooAuthError` is raised instead, so a bad credential is
distinguishable from every other refusal. Responses with status 429 or 5xx get a single
retry, honouring ``Retry-After`` when the site supplies one.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

WC_V3 = "wc/v3"
WC_ANALYTICS = "wc-analytics"

_REQUEST_TIMEOUT_S = 30.0
_RETRY_AFTER_CAP_S = 30.0
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_AUTH_STATUSES = frozenset({401, 403})
_SCHEMES = ("http://", "https://")
_AUTH_HINT = (
    "verify the consumer key and secret, confirm the key has read/write permission, and make "
    "sure the site is served over HTTPS or configured to accept Basic auth locally"
)


class WooApiError(RuntimeError):
    """Raised when the site rejects a request. ``code`` carries WordPress's machine-readable
    error slug (``woocommerce_rest_product_invalid_id`` and the like) and ``status`` the
    HTTP status. The message text comes from the site: fine to log, but it must be fenced
    before a model reads it."""

    def __init__(self, message: str, *, code: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class WooAuthError(WooApiError):
    """Status 401 or 403. Usual causes: a mistyped key or secret, a key whose user lacks
    ``manage_woocommerce``, or Basic auth sent over plain HTTP, which WooCommerce rejects
    unless the site has been configured to allow it."""


class WooExecutor(Protocol):
    """The transport surface the backend relies on. ``request`` yields the parsed JSON body
    together with the response headers; ``paged`` reads ``X-WP-TotalPages`` from those
    headers to know when to stop."""

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> tuple[Any, dict[str, str]]: ...


class Routes:
    """The REST paths this deployment reads and writes, relative to ``/wp-json/``. Keeping
    them here means a route rename touches one place and the local store can match the
    same strings."""

    site_index = ""
    system_status = f"{WC_V3}/system_status"
    products = f"{WC_V3}/products"
    categories = f"{WC_V3}/products/categories"
    reviews = f"{WC_V3}/products/reviews"
    orders = f"{WC_V3}/orders"
    revenue_stats = f"{WC_ANALYTICS}/reports/revenue/stats"

    @staticmethod
    def product(product_id: str | int) -> str:
        return f"{Routes.products}/{product_id}"

    @staticmethod
    def variations(product_id: str | int) -> str:
        return f"{Routes.products}/{product_id}/variations"

    @staticmethod
    def variation(product_id: str | int, variation_id: str | int) -> str:
        return f"{Routes.products}/{product_id}/variations/{variation_id}"


def normalize_store_url(raw: str) -> str:
    """Return the site URL with a scheme and without a trailing slash. A bare host is
    assumed to be HTTPS."""
    url = raw.strip().rstrip("/")
    if url and not url.lower().startswith(_SCHEMES):
        url = "https://" + url
    return url


def store_url_from_env(default: str = "http://localhost:8090") -> str:
    return normalize_store_url(os.environ.get("WOOCOMMERCE_STORE_URL") or default)


class WooRestClient:
    """The real network transport. The key and secret live inside the ``httpx`` auth object
    and nowhere else on the instance; ``repr`` shows only the store URL."""

    def __init__(
        self,
        store_url: str,
        *,
        consumer_key: str,
        consumer_secret: str,
        http: httpx.AsyncClient | None = None,
        retry_backoff: float = 1.0,
    ) -> None:
        self.store_url = normalize_store_url(store_url)
        self._retry_backoff = retry_backoff
        self._session = http
        if self._session is None:
            self._session = httpx.AsyncClient(
                auth=httpx.BasicAuth(consumer_key, consumer_secret),
                timeout=httpx.Timeout(_REQUEST_TIMEOUT_S),
                follow_redirects=True,
            )

    def __repr__(self) -> str:
        return f"WooRestClient({self.store_url!r})"

    async def aclose(self) -> None:
        assert self._session is not None
        await self._session.aclose()

    def _url(self, path: str) -> str:
        return f"{self.store_url}/wp-json/{path.lstrip('/')}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> tuple[Any, dict[str, str]]:
        assert self._session is not None
        query = {name: value for name, value in (params or {}).items() if value is not None}
        response = await self._session.request(method, self._url(path), params=query, json=json)
        if response.status_code in _RETRYABLE_STATUSES:
            # One retry only. A site that is still overloaded after that should surface
            # the error rather than have the portal hang on a request loop.
            await asyncio.sleep(self._retry_delay(response))
            response = await self._session.request(method, self._url(path), params=query, json=json)
        return _read_response(response)

    def _retry_delay(self, response: httpx.Response) -> float:
        advised = response.headers.get("Retry-After", "")
        if advised.isdigit():
            return min(float(advised), _RETRY_AFTER_CAP_S)
        return self._retry_backoff


def _read_response(response: httpx.Response) -> tuple[Any, dict[str, str]]:
    """Parse the JSON body and translate WordPress error envelopes into exceptions."""
    body: Any = None
    if response.content:
        try:
            body = response.json()
        except ValueError:
            body = None
    if response.status_code < 400:
        return body, dict(response.headers.items())
    envelope = body if isinstance(body, dict) else {}
    slug = str(envelope.get("code") or "")
    text = str(envelope.get("message") or "") or f"HTTP {response.status_code}"
    if response.status_code in _AUTH_STATUSES:
        raise WooAuthError(
            f"{text} (HTTP {response.status_code} from the store: {_AUTH_HINT})",
            code=slug,
            status=response.status_code,
        )
    raise WooApiError(text, code=slug, status=response.status_code)


def _total_pages(headers: dict[str, str]) -> int | None:
    for name in ("X-WP-TotalPages", "x-wp-totalpages"):
        if name in headers and str(headers[name]).isdigit():
            return int(headers[name])
    return None


async def paged(
    executor: WooExecutor,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    per_page: int = 100,
    max_items: int = 1000,
) -> list[dict[str, Any]]:
    """Walk a collection page by page and return up to ``max_items`` rows. WooCommerce
    allows at most 100 per page and reports the page count in ``X-WP-TotalPages``; the
    loop also stops on a short or empty page in case that header is absent."""
    rows: list[dict[str, Any]] = []
    base = dict(params or {})
    for page in itertools.count(1):
        body, headers = await executor.request(
            "GET", path, params={**base, "per_page": per_page, "page": page}
        )
        batch = body if isinstance(body, list) else []
        rows.extend(batch)
        last_page = _total_pages(headers)
        exhausted = not batch or len(batch) < per_page
        if exhausted or (last_page is not None and page >= last_page):
            break
        if len(rows) >= max_items:
            break
    return rows[:max_items]
