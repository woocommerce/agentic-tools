# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""Wiring for the merchant portal: settings and a transport in, a mounted ``APIRouter``
out.

The router itself comes from ``demo_common.build_merchant_router`` and is used exactly as
Anthropic's commerce-agents reference ships it. Approval gating, SSE streaming, the memory
endpoints, and card presentation all live there. This module contributes only the
WooCommerce-specific pieces: the store identity, the REST transport, the backend, and a
few extra values the portal's landing page displays.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from commerce_common.memory import MemoryStore
from demo_common import MerchantIdentity, build_merchant_router
from fastapi import APIRouter
from merchant_agent_runtime import MerchantAgent

from .agent_config import DATA_DIR, SKILLS_DIR, WooSettings, build_merchant_config
from .local_store import LocalStore
from .rest_client import WooExecutor, WooRestClient
from .store_view import WooStoreView
from .woo_backend import WooMerchantBackend

SEED_FILE = DATA_DIR / "seed.json"


@dataclass(frozen=True)
class MerchantPortal:
    """Handles ``main.py`` keeps after construction: the router to mount, the backend to
    warm, and the HTTP client to close on shutdown."""

    router: APIRouter
    backend: WooMerchantBackend
    # Left as None when nothing needs closing: the in-memory store, or an executor injected
    # by the caller (the test suite passes a scripted one).
    client: WooRestClient | None


def _transport(
    settings: WooSettings, injected: WooExecutor | None
) -> tuple[WooExecutor, WooRestClient | None]:
    """Pick the executor: an injected one wins, then the seeded in-memory store when
    ``settings.local_store`` is set, otherwise a real client the caller must close."""
    if injected is not None:
        return injected, None
    if settings.local_store:
        return LocalStore.from_seed(SEED_FILE, store_url=settings.store_url), None
    client = WooRestClient(
        settings.store_url,
        consumer_key=settings.consumer_key,
        consumer_secret=settings.consumer_secret,
    )
    return client, client


def _overview_extras(
    backend: WooMerchantBackend, settings: WooSettings
) -> Callable[[], dict[str, Any]]:
    """Values the portal's landing page shows beyond the shared router's own: sparkline
    series, suggestion cards, and which transport produced the numbers, so a local-store
    demo is not mistaken for a live WooCommerce site."""
    store_kind = "local" if settings.local_store else "woocommerce"

    def extras() -> dict[str, Any]:
        return {
            "trends": backend.kpi_trends(),
            "insights": backend.home_insights(),
            "store_url": settings.store_url,
            "store_kind": store_kind,
        }

    return extras


def create_merchant_portal(
    settings: WooSettings,
    memory_store: MemoryStore,
    *,
    executor: WooExecutor | None = None,
) -> MerchantPortal:
    """Build the portal for a single store.

    Credential flow: the key and secret are handed to ``WooRestClient`` and stop there. The
    backend owns the client, the agent owns the backend, and tool results are built from
    parsed responses, so nothing the model sees can include the secret.

    Transport selection: a real site gets ``WooRestClient``; ``settings.local_store`` swaps
    in ``LocalStore`` built from ``merchant/data/seed.json``, the file ``seed_store.py``
    also loads into a real site; and an explicit ``executor`` argument overrides both, which
    is how the tests drive these routes."""
    transport, client = _transport(settings, executor)
    config = build_merchant_config(settings.store_name or settings.merchant_id)
    backend = WooMerchantBackend(transport, settings, config)
    agent = MerchantAgent(
        backend=backend, skills_dir=SKILLS_DIR, config=config, memory_store=memory_store
    )
    identity = MerchantIdentity(merchant_id=settings.merchant_id, operator=settings.operator)
    router = build_merchant_router(
        storefront=WooStoreView(backend),
        backend=backend,
        agent=agent,
        identity=identity,
        example_dir="merchant",
        overview_extras=_overview_extras(backend, settings),
    )
    return MerchantPortal(router=router, backend=backend, client=client)
