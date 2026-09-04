// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Adapted from Anthropic's retail storefront example (Apache-2.0).

"use client";

import { useId } from "react";
import { formatMoney, optionValuesLabel, safeHandoffs, useCatalogIndex } from "web-shared";
import { fetchProducts } from "@/lib/api";
import type { CheckoutPayload, Product } from "@/lib/types";
import { CheckoutAction, CheckoutFootnote } from "../CheckoutAction";
import { ProductImage } from "../ProductTile";

/** The staged checkout: the lines, the subtotal, and the way to the store's checkout page. */
export default function CheckoutSummary({ payload }: { payload: CheckoutPayload }) {
  const cart = payload.cart;
  // The backend's own link, for when the live cart has not reported one yet.
  const fallbackHref = safeHandoffs(payload.handoffs)[0]?.url ?? null;
  // Several cards can be on the page at once, so each gets its own note id.
  const noteId = useId();
  // Lines carry title, price, and quantity; thumbnails come from the catalog when a line has none.
  const catalog = useCatalogIndex(fetchProducts);
  return (
    <section data-checkout-card className="rounded-2xl border-2 border-(--accent) bg-(--card) p-4 shadow-(--shadow-sm)">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-[15px] font-semibold text-(--ink)">Ready to check out</h3>
        <div className="flex items-center gap-1.5">
          <span className="whitespace-nowrap rounded-full border border-(--line) bg-(--well)/60 px-2.5 py-0.5 text-[11px] font-semibold text-(--ink-soft)">Not charged</span>
          {payload.fulfillment_method ? (
            <span className="rounded-full bg-(--accent-soft) px-2.5 py-0.5 text-[13px] font-semibold capitalize text-(--ink)">{payload.fulfillment_method}</span>
          ) : null}
        </div>
      </div>
      {payload.note ? <p className="mt-1 text-[13px] text-(--ink-soft)">{payload.note}</p> : null}
      <div className="mt-3 space-y-2 rounded-lg bg-(--well)/60 p-3 text-sm">
        {cart.items.map((item) => {
          const product: Product = catalog[item.product_id] ?? {
            product_id: item.product_id,
            title: item.title,
            price: item.price,
            image_url: item.image_url,
          };
          const chosen = optionValuesLabel(item);
          return (
            <div key={item.product_id} className="flex items-center gap-2.5">
              <ProductImage product={{ ...product, image_url: item.image_url ?? product.image_url }} className="h-10 w-10 shrink-0 rounded-lg !text-xl" />
              <div className="min-w-0 flex-1">
                <div className="flex justify-between gap-2">
                  <span className="line-clamp-1 text-(--ink)" title={item.title}>
                    {item.title} × {item.quantity}
                  </span>
                  <span className="shrink-0 text-(--ink)">{formatMoney(item.line_total, cart.currency)}</span>
                </div>
                {chosen ? <div className="text-[11.5px] text-(--ink-soft)">{chosen}</div> : null}
              </div>
            </div>
          );
        })}
        <div className="flex justify-between border-t border-(--line) pt-1.5 text-base font-bold text-(--ink)">
          <span>Subtotal</span>
          <span>{formatMoney(cart.subtotal, cart.currency)}</span>
        </div>
        <p className="text-[11px] leading-snug text-(--ink-soft)">Shipping, tax, and any coupons are worked out on the store's checkout page.</p>
      </div>
      <CheckoutAction fallbackHref={fallbackHref} describedBy={noteId} />
      <CheckoutFootnote id={noteId} />
    </section>
  );
}
