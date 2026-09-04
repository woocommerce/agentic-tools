# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""The storefront smoke walk-through reports capabilities without printing them."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def load_smoke():
    path = Path(__file__).resolve().parents[2] / "scripts" / "smoke.py"
    spec = importlib.util.spec_from_file_location("storefront_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_handoff_step_does_not_print_the_bearer_url() -> None:
    secret = "cart-secret-capability"

    class Backend:
        async def get_cart(self, shopper):
            return object()

        async def checkout_handoff(self, shopper, cart):
            return [SimpleNamespace(url=f"https://shop.example/checkout?cart={secret}")]

    smoke = load_smoke()
    walkthrough = object.__new__(smoke.Walkthrough)
    walkthrough.backend = Backend()
    walkthrough.shopper = object()

    line = await walkthrough.handoff()

    assert line == "https://shop.example/checkout?cart=<redacted>"
    assert secret not in line
