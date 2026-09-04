# Copyright 2026 Automattic Inc.
# SPDX-License-Identifier: Apache-2.0

"""What the web app needs to look like the shop: name, tagline, logo, and a colour pair.

Two reads build the payload. WordPress's root index (``/wp-json/``) gives the site name,
its tagline, the site icon URL, and the attachment id of a custom logo, which ``wp/v2/media``
resolves to a URL. When the bridge plugin is active, its ``/brand`` route adds the theme's
global background and text colours. The colour pair is kept only when it clears WCAG's
3:1 ratio for UI components; below that the web app's own stylesheet defaults are the
better choice.

The browser never talks to WordPress: the host's ``/api/brand`` route calls
:class:`BrandSource`, which remembers a good answer for ``ttl`` seconds and answers a
failed read with fallbacks it does not remember, so the next request tries again.
"""

from __future__ import annotations

import os
import time
from typing import Any

from .store_client import BRIDGE_V1, WP_V2, StoreApiClient

CACHE_TTL = 600.0
MIN_CONTRAST = 3.0
FALLBACK_TAGLINE = "An assistant that knows this shop's shelves."

# sRGB channel weights for relative luminance, red/green/blue.
_LUMA_WEIGHTS = (0.2126, 0.7152, 0.0722)


def parse_hex_colour(value: str | None) -> tuple[int, int, int] | None:
    """``#rgb`` or ``#rrggbb`` to an (r, g, b) triple of 0-255 ints; None for anything
    else (a CSS variable, a named colour, an empty string)."""
    if not value:
        return None
    hex_part = value.strip().removeprefix("#")
    if len(hex_part) == 3:
        hex_part = "".join(2 * ch for ch in hex_part)
    if len(hex_part) != 6:
        return None
    try:
        packed = int(hex_part, 16)
    except ValueError:
        return None
    return (packed >> 16) & 0xFF, (packed >> 8) & 0xFF, packed & 0xFF


def contrast_ratio(first: str | None, second: str | None) -> float | None:
    """WCAG 2 contrast ratio between two hex colours, 1.0 (identical) to 21.0 (black on
    white); None when either colour cannot be parsed."""
    lumas: list[float] = []
    for colour in (first, second):
        rgb = parse_hex_colour(colour)
        if rgb is None:
            return None
        total = 0.0
        for weight, channel in zip(_LUMA_WEIGHTS, rgb, strict=True):
            fraction = channel / 255
            if fraction > 0.04045:
                linear = pow((fraction + 0.055) / 1.055, 2.4)
            else:
                linear = fraction / 12.92
            total += weight * linear
        lumas.append(total)
    bright, dim = sorted(lumas, reverse=True)
    return (bright + 0.05) / (dim + 0.05)


def readable_pair(background: str | None, foreground: str | None) -> bool:
    ratio = contrast_ratio(background, foreground)
    return ratio is not None and ratio >= MIN_CONTRAST


class BrandSource:
    """The brand payload for one site, remembered for ``ttl`` seconds after a good read."""

    def __init__(self, client: StoreApiClient, ttl: float = CACHE_TTL) -> None:
        self.client = client
        self.ttl = ttl
        self._remembered: tuple[float, dict[str, Any]] | None = None

    def forget(self) -> None:
        self._remembered = None

    async def brand(self) -> dict[str, Any]:
        if self._remembered and self._remembered[0] > time.monotonic():
            return self._remembered[1]
        root = await self._json("")
        if not root:
            return self._compose({}, None, {})
        media = (
            await self._json(f"{WP_V2}/media/{root['site_logo']}", {"_fields": "source_url"})
            if root.get("site_logo")
            else None
        )
        bridge = (
            await self._json(f"{BRIDGE_V1}/brand")
            if BRIDGE_V1 in (root.get("namespaces") or [])
            else {}
        )
        payload = self._compose(root, media, bridge)
        self._remembered = (time.monotonic() + self.ttl, payload)
        return payload

    async def _json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """A GET whose failure is an empty dict: branding is decoration, never an error."""
        try:
            body, _ = await self.client.get(path, params)
        except Exception:  # noqa: BLE001 - any failure means "no branding from this route"
            return {}
        return body if isinstance(body, dict) else {}

    def _compose(
        self, root: dict[str, Any], media: dict[str, Any] | None, bridge: dict[str, Any]
    ) -> dict[str, Any]:
        tagline = bridge.get("tagline") or root.get("description") or None
        logo = (
            bridge.get("logo_url") or (media or {}).get("source_url") or root.get("site_icon_url")
        )
        payload: dict[str, Any] = {
            "name": root.get("name") or self.client.store_url,
            "slogan": tagline,
            "tagline": tagline or os.environ.get("BRAND_TAGLINE") or FALLBACK_TAGLINE,
            "short_description": None,
            "logo_url": logo or None,
            "cover_image_url": None,
        }
        colours = bridge.get("colors") or {}
        background, foreground = colours.get("background"), colours.get("foreground")
        if readable_pair(background, foreground):
            payload["colors"] = {"background": background, "foreground": foreground}
        return payload
