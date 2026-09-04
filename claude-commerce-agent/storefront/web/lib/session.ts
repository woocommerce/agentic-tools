// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Written for the WooCommerce storefront; the retail example's sessions are per demo profile
// and never outlive a page load.

"use client";

import { useEffect, useState } from "react";
import { AgentApi } from "web-shared";
import { attachCart, fetchCart } from "./api";
import type { CartPayload } from "./types";

/** The session id lives with the tab; the cart token outlives it. */
export const SESSION_KEY = "woocommerce-storefront-session";
export const CART_TOKEN_KEY = "woocommerce-storefront-cart-token";
/** `?cart=<token>` on any page joins that cart, so a link can carry a cart across browsers. */
const CART_PARAM = "cart";

function read(storage: () => Storage, key: string): string | null {
  try {
    return storage().getItem(key);
  } catch {
    return null;
  }
}

function write(storage: () => Storage, key: string, value: string | null): void {
  try {
    if (value === null) storage().removeItem(key);
    else storage().setItem(key, value);
  } catch {
    // Storage can be unavailable (private mode, blocked site data); the session still works for this load.
  }
}

export function rememberCartToken(token: string | null): void {
  if (token) write(() => window.localStorage, CART_TOKEN_KEY, token);
}

function cartTokenFromUrl(): string | null {
  return new URL(window.location.href).searchParams.get(CART_PARAM);
}

function stripCartParam(): void {
  const url = new URL(window.location.href);
  if (!url.searchParams.has(CART_PARAM)) return;
  url.searchParams.delete(CART_PARAM);
  window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
}

export interface StoreSession {
  /** Null until the session is open, and after opening it failed. */
  sessionId: string | null;
  /** The cart as it stood when the session opened; the shell takes it as its first cart. */
  startupCart: CartPayload | null;
  /** The API could not be reached or refused to start a session. */
  failed: boolean;
}

/** A stored id is alive when the host still answers GET /api/cart for it. */
async function revalidate(root: string, sessionId: string): Promise<CartPayload | null> {
  const probe = new AgentApi(root, "/api");
  probe.session = sessionId;
  return probe.fetchCart<CartPayload>();
}

/**
 * Opens the guest session: a stored id is reused while the host still knows it, otherwise a new
 * one is started and the cart the browser remembers (or the one a `?cart=` link names) is attached
 * to it. `current` says whether this attempt is still the one the page wants; a superseded attempt
 * writes nothing.
 */
async function openSession(api: AgentApi, current: () => boolean): Promise<StoreSession | null> {
  const stored = read(() => window.sessionStorage, SESSION_KEY);
  const linkedToken = cartTokenFromUrl();
  let sessionId: string | null = null;
  let cart: CartPayload | null = null;
  let fresh = false;

  if (stored) {
    cart = await revalidate(api.root, stored);
    if (cart) sessionId = stored;
  }
  if (!sessionId) {
    const probe = new AgentApi(api.root, "/api");
    const started = await probe.startSession({ user_id: "guest" });
    if (!started) return null;
    sessionId = started.sessionId;
    fresh = true;
  }
  if (!current()) return null;
  api.session = sessionId;
  write(() => window.sessionStorage, SESSION_KEY, sessionId);

  // A link's token wins; the remembered one only matters when the session is new, since a
  // revalidated session is already bound to its cart.
  const token = linkedToken ?? (fresh ? read(() => window.localStorage, CART_TOKEN_KEY) : null);
  if (token) {
    const attached = await attachCart(token);
    if (!current()) return null;
    if (attached) cart = attached;
    else if (!linkedToken) write(() => window.localStorage, CART_TOKEN_KEY, null);
  }
  if (linkedToken) stripCartParam();
  if (!cart) {
    cart = await fetchCart();
    if (!current()) return null;
  }
  return { sessionId, startupCart: cart, failed: false };
}

const attempts = new WeakMap<AgentApi, number>();

export function useStoreSession(api: AgentApi): StoreSession {
  const [session, setSession] = useState<StoreSession>({ sessionId: null, startupCart: null, failed: false });

  useEffect(() => {
    // Strict mode runs the effect twice; only the latest attempt may install its session.
    const attempt = (attempts.get(api) ?? 0) + 1;
    attempts.set(api, attempt);
    const current = () => attempts.get(api) === attempt;
    void openSession(api, current).then((opened) => {
      if (!current()) return;
      if (opened) setSession(opened);
      else {
        api.session = null;
        setSession({ sessionId: null, startupCart: null, failed: true });
      }
    });
    return () => {
      attempts.set(api, (attempts.get(api) ?? 0) + 1);
    };
  }, [api]);

  return session;
}
