# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""Environment-driven settings for the WooCommerce merchant deployment, plus the
``MerchantAgentConfig`` handed to the agent. No other module under ``merchant/api``
touches ``os.environ``.

Variables read:

    WOOCOMMERCE_LOCAL_STORE        set to 1 to serve everything from ``api/local_store.py``
                                   in memory; the remaining variables become optional
    WOOCOMMERCE_STORE_URL          site URL including the scheme, e.g. https://store.example
    WOOCOMMERCE_CONSUMER_KEY       a WooCommerce REST key (``ck_…``) with read/write access,
                                   or a WordPress login when used with an application
                                   password
    WOOCOMMERCE_CONSUMER_SECRET    the matching secret (``cs_…``), or the application
                                   password
    WOOCOMMERCE_OPERATOR           name recorded in the change ledger as the approving person
    WOOCOMMERCE_STORE_NAME         overrides the site title shown in the portal
    WOOCOMMERCE_LOW_STOCK_DEFAULT  low-stock threshold for products with no category or slug
                                   rule of their own
    WOOCOMMERCE_FULFILMENT_SLA_DAYS how long a paid order may stay in ``processing`` before
                                   the delayed-order alert fires
    WOOCOMMERCE_DISABLE_ANALYTICS  set to 1 to bypass ``wc-analytics`` and compute metrics
                                   from the order scan alone
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from demo_common import host_approval_default
from merchant_agent import MerchantAgentConfig

from .rest_client import normalize_store_url

EXAMPLE_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = EXAMPLE_ROOT / "data"
# Skill files for the merchant agent. They are vendored under vendor/skills next to the
# shopping agent's set (NOTICE records their origin). The Python packages come from the
# pinned dependency; skills are plain files the runtime loads from disk.
SKILLS_DIR = EXAMPLE_ROOT.parent / "vendor" / "skills" / "merchant"

# Placeholder URL for the in-memory store. Deliberately not a real hostname, so anyone
# reading a log line can tell no network request was involved.
LOCAL_STORE_URL = "http://acme-supply.local"

_PREFIX = "WOOCOMMERCE_"
_FALSE_FLAGS = frozenset({"", "0", "false"})
_SETUP_HELP = (
    "WOOCOMMERCE_STORE_URL, WOOCOMMERCE_CONSUMER_KEY, and WOOCOMMERCE_CONSUMER_SECRET are "
    "required in merchant/.env (start from merchant/.env.example). Alternatively set "
    "WOOCOMMERCE_LOCAL_STORE=1 to serve an in-memory store with no WooCommerce site. For a "
    "live site, create a REST API key with read/write permission under WooCommerce → "
    "Settings → Advanced → REST API, or use a WordPress application password; "
    "wordpress/local-store/ brings up a throwaway site in Docker."
)


class MissingCredentials(RuntimeError):
    """Store URL or credential missing from the environment. The message spells out which
    variables to set and where."""


class _Env:
    """Typed reads of the ``WOOCOMMERCE_*`` variables. Every accessor takes the suffix
    only, so the variable names above are the single place the prefix is spelled."""

    @staticmethod
    def text(suffix: str, default: str = "") -> str:
        return (os.environ.get(_PREFIX + suffix) or default).strip()

    @staticmethod
    def flag(suffix: str) -> bool:
        return _Env.text(suffix, "0") not in _FALSE_FLAGS

    @staticmethod
    def count(suffix: str, default: int) -> int:
        return int(_Env.text(suffix) or default)


@dataclass(frozen=True)
class WooSettings:
    """Connection settings for a single WooCommerce site. ``consumer_key`` and
    ``consumer_secret`` are excluded from the dataclass repr, so a settings object can be
    logged without exposing them; ``api/rest_client.py`` is the only consumer of the
    secret."""

    store_url: str
    consumer_key: str = field(repr=False)
    consumer_secret: str = field(repr=False)
    # Set when the transport is ``api/local_store.py`` instead of a live site. Key and
    # secret are blank in that case; there is nothing to authenticate against.
    local_store: bool
    operator: str
    store_name: str | None
    low_stock_default: int
    fulfilment_sla_days: int
    analytics_enabled: bool

    @property
    def merchant_id(self) -> str:
        host = self.store_url
        for scheme in ("https://", "http://"):
            host = host.removeprefix(scheme)
        return host


def local_store_requested() -> bool:
    return _Env.flag("LOCAL_STORE")


def load_settings() -> WooSettings:
    local = local_store_requested()
    if local:
        # In local mode there is no site and no secret. A placeholder URL is kept so the
        # portal header and the ledger entries still have a store to name.
        url, key, secret = LOCAL_STORE_URL, "", ""
    else:
        url = normalize_store_url(_Env.text("STORE_URL"))
        key, secret = _Env.text("CONSUMER_KEY"), _Env.text("CONSUMER_SECRET")
        if not (url and key and secret):
            raise MissingCredentials(_SETUP_HELP)
    return WooSettings(
        store_url=url,
        consumer_key=key,
        consumer_secret=secret,
        local_store=local,
        operator=_Env.text("OPERATOR") or "Operator",
        store_name=_Env.text("STORE_NAME") or None,
        low_stock_default=_Env.count("LOW_STOCK_DEFAULT", 8),
        fulfilment_sla_days=_Env.count("FULFILMENT_SLA_DAYS", 3),
        analytics_enabled=_Env.text("DISABLE_ANALYTICS", "0") != "1",
    )


def build_merchant_config(store_name: str) -> MerchantAgentConfig:
    """Guardrail and approval configuration for this deployment. ``enable_campaigns`` stays
    on: the store lists its marketing extensions' campaigns through ``wc-admin``, so the
    read tool has something to return, and ``stage_campaign`` refuses with the reason
    (WooCommerce has no API to create one). ``enable_analysis`` is False because there is
    no SQL endpoint for ``execute_analysis_query`` to run against."""
    return MerchantAgentConfig(
        brand_name=store_name,
        approval_surface="the Approve button on the staged change card",
        require_host_approval=host_approval_default(),
        enable_analysis=False,
        enable_campaigns=True,
    )
