# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""FastAPI entry point for the WooCommerce merchant portal.

    uvicorn merchant.api.main:app --reload --port 8007

Only the merchant side runs in this process. There is no separate storefront to mirror:
the catalog, orders, and stock the portal shows are read from the WooCommerce site over
``wc/v3``, and approved changes are written back through the same API. The buyer-facing
``storefront/`` service is a different process.

Settings load from ``merchant/.env`` (see ``merchant/.env.example``) and then from the
repository-root ``.env`` for the Anthropic API key both services share. If the WooCommerce
credential is absent the process still boots, and ``/api/merchant/health`` reports what
needs setting.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from commerce_common.memory import JsonFileMemoryStore
from demo_common import load_demo_env
from demo_common.host import build_app
from fastapi import FastAPI

from .agent_config import DATA_DIR, EXAMPLE_ROOT, MissingCredentials, load_settings
from .merchant import MerchantPortal, create_merchant_portal

logger = logging.getLogger(__name__)

API_TITLE = "WooCommerce merchant example API"
API_PREFIX = "/api/merchant"
MEMORY_FILE = DATA_DIR / ".memory-store.json"

load_demo_env(EXAMPLE_ROOT)


def _unconfigured_app(reason: str) -> FastAPI:
    """Minimal app used when settings are incomplete. It answers the health route with the
    reason, so the operator sees a clear message instead of an import-time crash."""
    logger.warning("merchant API running unconfigured: %s", reason)
    app = build_app(title=f"{API_TITLE} (unconfigured)")

    @app.get(f"{API_PREFIX}/health")
    async def health() -> dict:
        return {"ok": False, "role": "merchant", "error": reason}

    return app


async def _prime(portal: MerchantPortal, store_url: str) -> None:
    """Warm the caches at startup: store profile, product catalog, and order scan. The
    portal's overview renders from these caches, and hitting the API early surfaces a bad
    credential before any user request does. If the site is unreachable the error is
    logged and startup continues; the health route then carries the explanation."""
    backend = portal.backend
    try:
        await backend.warm()
    except Exception:
        logger.exception("startup read of %s failed", store_url)
        return
    logger.info(
        "%s reachable; %d products cached", backend.store_name, len(backend.catalog.cached())
    )


def _mount(app: FastAPI, portal: MerchantPortal, store_url: str) -> None:
    """Attach the portal's router and wrap the shared lifespan with cache warming on the
    way in and client shutdown on the way out."""
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(scope: FastAPI) -> AsyncIterator[None]:
        async with inner(scope):
            await _prime(portal, store_url)
            try:
                yield
            finally:
                if portal.client is not None:
                    await portal.client.aclose()

    app.router.lifespan_context = lifespan
    app.include_router(portal.router, prefix=API_PREFIX)


def create_app() -> FastAPI:
    try:
        settings = load_settings()
    except MissingCredentials as error:
        return _unconfigured_app(str(error))
    app = build_app(title=API_TITLE)
    portal = create_merchant_portal(settings, JsonFileMemoryStore(MEMORY_FILE))
    _mount(app, portal, settings.store_url)
    return app


app = create_app()
