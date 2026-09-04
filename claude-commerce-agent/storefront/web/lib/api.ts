// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Adapted from Anthropic's retail storefront example (Apache-2.0).

import { AgentApi } from "web-shared";
import type { Brand, CartPayload, Product, ProductDetails } from "./types";

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8006";

export const api = new AgentApi(API_URL, "/api");

export const UNREACHABLE =
  "Couldn't reach the storefront API on port 8006. Start it with " +
  "`uvicorn storefront.api.main:app --port 8006` and try again.";

export async function fetchProducts(): Promise<Product[] | null> {
  const data = await api.get<{ products: Product[] }>("/products", { limit: "100" });
  return data?.products ?? null;
}

/** Ids are numeric strings here, but they are encoded like any other path segment. */
export function fetchProduct(productId: string): Promise<ProductDetails | null> {
  return api.get<ProductDetails>(`/products/${encodeURIComponent(productId)}`);
}

export function fetchBrand(): Promise<Brand | null> {
  return api.get<Brand>("/brand");
}

export function fetchCart(): Promise<CartPayload | null> {
  return api.fetchCart<CartPayload>();
}

/** Joins a cart the browser already holds; null when the store no longer accepts the token (404). */
export function attachCart(cartToken: string): Promise<CartPayload | null> {
  return api.post<CartPayload>("/cart/attach", { cart_token: cartToken });
}

export type AddResult = { ok: true; cart: CartPayload } | { ok: false; reason: string };

const ADD_FAILED = "The store didn't accept that. Try again, or ask the assistant to add it.";
const NOT_SURFACED =
  "The assistant can only add products it has shown you in this conversation. Ask about this one first, then add it.";

/**
 * The add button's write. It goes through the same gates as the assistant's own adds, so the
 * host answers 400 when the product has not come up in this session; the detail is read so the
 * page can say why instead of just failing.
 */
export async function addToCart(productId: string, quantity = 1): Promise<AddResult> {
  let response: Response;
  try {
    response = await fetch(`${api.base}/cart/add`, {
      method: "POST",
      headers: api.headers(true),
      body: JSON.stringify({ product_id: productId, quantity }),
    });
  } catch {
    return { ok: false, reason: UNREACHABLE };
  }
  if (response.ok) {
    const data = (await response.json()) as {
      cart?: Omit<CartPayload, "checkout_url" | "cart_token"> | null;
      checkout_url?: string | null;
      cart_token?: string | null;
    };
    if (!data.cart) return { ok: false, reason: ADD_FAILED };
    return {
      ok: true,
      cart: { ...data.cart, checkout_url: data.checkout_url ?? null, cart_token: data.cart_token ?? null },
    };
  }
  let detail = "";
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") detail = body.detail;
  } catch {
    // A non-JSON error body carries nothing to show.
  }
  if (/not in this session/i.test(detail)) return { ok: false, reason: NOT_SURFACED };
  return { ok: false, reason: detail || ADD_FAILED };
}
