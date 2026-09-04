// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Written for the WooCommerce storefront: checkout is a handoff to the store's own page.

"use client";

import { useStoreFrame } from "web-shared";
import { CHECKOUT_FOOTNOTE, CHECKOUT_PROMPT, checkoutHref, checkoutLabel } from "@/lib/checkout";
import { useStore } from "@/lib/store";

/**
 * The primary action wherever the cart is summed up. With a checkout link and something in the
 * cart it opens the store's checkout in a new tab; otherwise it asks the assistant to stage one.
 * `fallbackHref` lets a checkout card offer the link the backend put on it when the live cart has
 * none yet.
 */
export function CheckoutAction({ fallbackHref, describedBy }: { fallbackHref?: string | null; describedBy?: string }) {
  const { cart, brand } = useStore();
  const { ask } = useStoreFrame();
  const empty = !cart?.items.length;
  const href = checkoutHref(cart) ?? (empty ? null : fallbackHref ?? null);
  if (href) {
    return (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        aria-describedby={describedBy}
        className="btn-brand mt-3 flex w-full items-center justify-center gap-1.5 text-center"
      >
        {checkoutLabel(brand)}
        <span aria-hidden>↗</span>
      </a>
    );
  }
  return (
    <button type="button" onClick={() => ask(CHECKOUT_PROMPT)} disabled={empty} aria-describedby={describedBy} className="btn-primary mt-3 w-full">
      Check out
    </button>
  );
}

export function CheckoutFootnote({ id, className = "" }: { id?: string; className?: string }) {
  return (
    <p id={id} className={`mt-2 text-center text-[11px] leading-snug text-(--ink-soft)/80 ${className}`}>
      {CHECKOUT_FOOTNOTE}
    </p>
  );
}
