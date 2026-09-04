# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""FastAPI host for the WooCommerce storefront.

:class:`StorefrontService` wires one site's Store API client, the ``WooStorefrontBackend``
over it, the brand reader, and the shopping agent into the shared storefront host from
Anthropic's commerce-agents reference. The site comes from ``WOOCOMMERCE_STORE_URL`` (the
local Docker store by default) and an optional ``WOOCOMMERCE_STORE_NAME``.

    uvicorn storefront.api.main:app --reload --port 8006

Startup probes the site for its name and for the bridge plugin, rebuilds the agent's
prompt from what it learns, and pre-fills the product grid in the background
(``CATALOG_WARMUP=0`` skips that). Beyond the reference's routes, the host adds
``/api/cart/add`` (the grid's button), ``/api/cart/attach`` (a cart token the page already
holds), and ``/api/brand``. Every cart payload carries ``checkout_url`` and ``cart_token``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from commerce_common.memory import InMemoryMemoryStore
from demo_common import (
    REPO_ROOT,
    CartAddRequest,
    MemorySeeder,
    build_storefront_host,
    load_demo_env,
)
from fastapi import HTTPException
from pydantic import BaseModel, Field
from shopping_agent_runtime import ShoppingAgent

from .agent_config import shopping_config_for
from .brand import BrandSource
from .catalog_warmup import preload_grid
from .store_client import StoreApiClient, store_url_from_env
from .woo_backend import WooStorefrontBackend

logger = logging.getLogger(__name__)

STOREFRONT_ROOT = Path(__file__).resolve().parents[1]
MEMORY_SEED = STOREFRONT_ROOT / "data" / "memory-seed.json"
SKILLS_DIR = REPO_ROOT / "vendor" / "skills" / "shopping"
ADD_BUTTON_NOTE = (
    "The shopper used the add-to-cart button for {title} ({product_id}), quantity {quantity}."
)


class CartAttachRequest(BaseModel):
    """A Store API cart token the page brought along."""

    cart_token: str = Field(min_length=1, max_length=2048)


class StorefrontService:
    """One WooCommerce site's storefront: client, backend, brand reader, agent, and host."""

    def __init__(self, store_url: str, store_name: str | None = None) -> None:
        self.client = StoreApiClient(store_url)
        self.backend = WooStorefrontBackend(self.client, store_name=store_name)
        self.brand = BrandSource(self.client)
        self.memory = InMemoryMemoryStore()
        self.host = build_storefront_host(
            title="WooCommerce store API",
            example_root=STOREFRONT_ROOT,
            backend=self.backend,
            agent=self.agent_for(bridge=False),
            memory_seeder=MemorySeeder(MEMORY_SEED),
            cart_extras=self.cart_extras,
        )
        self.app = self.host.app
        self.grid_task: asyncio.Task | None = None
        self._wrap_lifespan()

    def agent_for(self, *, bridge: bool) -> ShoppingAgent:
        """The agent's prompt is fixed at construction and names the store and whether order
        reads work, so the startup probe builds a second agent once it knows both."""
        return ShoppingAgent(
            backend=self.backend,
            skills_dir=SKILLS_DIR,
            config=shopping_config_for(self.backend.store_name, bridge=bridge),
            memory_store=self.memory,
        )

    def cart_extras(self, record) -> dict:
        """Added to every cart payload: where checkout continues, and the token the page
        may keep to bring this cart into a later session."""
        session_id = record.session_id
        return {
            "checkout_url": self.backend.checkout_url_for(session_id),
            "cart_token": self.backend.cart_token_for(session_id),
        }

    async def startup(self) -> None:
        await self.backend.probe()
        bridge = bool(self.backend.bridge_available)
        if bridge or self.backend.store_name != self.host.agent.config.brand_name:
            self.host.agent = self.agent_for(bridge=bridge)
        logger.info(
            "serving %s (%s) with the bridge plugin %s",
            self.client.store_url,
            self.backend.store_name,
            "present" if bridge else "absent",
        )
        self.grid_task = asyncio.create_task(preload_grid(self.backend))

    def _wrap_lifespan(self) -> None:
        """The shared host installs its own lifespan; ours runs inside it so the probe
        happens before the first request and the HTTP client closes on shutdown."""
        inner = self.app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app_):
            async with inner(app_):
                await self.startup()
                try:
                    yield
                finally:
                    await self.client.aclose()

        self.app.router.lifespan_context = lifespan


load_demo_env(STOREFRONT_ROOT)
service = StorefrontService(store_url_from_env(), os.environ.get("WOOCOMMERCE_STORE_NAME"))
app = service.app


@app.post("/api/cart/add")
async def cart_add(request: CartAddRequest, record: service.host.CurrentSession) -> dict:
    """The grid's button, run through the same executor as the agent's own adds."""
    return await service.host.direct_add(record, request, note=ADD_BUTTON_NOTE)


@app.post("/api/cart/attach")
async def cart_attach(body: CartAttachRequest, record: service.host.CurrentSession) -> dict:
    """Continue in a cart the shopper already has instead of opening a private one. 404
    when WooCommerce does not accept the token."""
    cart = await service.backend.attach_cart(record.session_id, body.cart_token)
    if cart is None:
        raise HTTPException(status_code=404, detail="The store doesn't accept that cart token")
    return await service.host.cart_payload(record)


@app.get("/api/brand")
async def brand() -> dict:
    return await service.brand.brand()
