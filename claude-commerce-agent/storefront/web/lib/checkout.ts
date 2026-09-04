// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Written for the WooCommerce storefront.

import type { Brand, CartPayload } from "./types";

export const CHECKOUT_PROMPT = "Check out my cart.";

export const CHECKOUT_FOOTNOTE =
  "Payment happens on the store's own WooCommerce checkout page — nothing is charged here.";

/**
 * The checkout link to offer: the cart's own URL, only while there is something to buy. The URL
 * comes from the host, never from the model; the scheme check keeps anything odd off an anchor.
 */
export function checkoutHref(cart: Pick<CartPayload, "items" | "checkout_url"> | null): string | null {
  const url = cart?.checkout_url;
  if (!url || !cart?.items.length) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? url : null;
  } catch {
    return null;
  }
}

export function checkoutLabel(brand: Brand | null): string {
  return brand?.name ? `Check out at ${brand.name}` : "Check out on the store";
}
