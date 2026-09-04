// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Adapted from Anthropic's retail storefront example (Apache-2.0); the docked panel became a
// drawer, and checkout became a link to the store.

"use client";

import { useEffect, useRef } from "react";
import { AskLink, BagPanel, formatMoney, optionValuesLabel, plural, RemoveLink, Stepper, TotalRow, useCatalogIndex, useStoreFrame } from "web-shared";
import { fetchProducts } from "@/lib/api";
import { useStore } from "@/lib/store";
import type { CartItem, Product } from "@/lib/types";
import { CheckoutAction, CheckoutFootnote } from "./CheckoutAction";
import { ProductImage, ProductTitle } from "./ProductTile";

/** The catalog record behind a line, or one built from the line when the grid never had it. */
function asProduct(item: CartItem, catalog: Record<string, Product>): Product {
  const family = item.variant_of ? catalog[item.variant_of] : undefined;
  return (
    catalog[item.product_id] ?? {
      ...family,
      product_id: item.product_id,
      title: item.title,
      price: item.price,
      image_url: item.image_url ?? family?.image_url,
      options: undefined,
      option_values: item.option_values,
    }
  );
}

/** How a message to the assistant names a line: the title, plus the chosen values on a variation. */
function lineName(item: CartItem): string {
  const chosen = optionValuesLabel(item);
  return chosen ? `${item.title} (${chosen})` : item.title;
}

function CartLines() {
  const { cart, assistantName } = useStore();
  const { ask } = useStoreFrame();
  const items = cart?.items ?? [];
  const count = cart?.item_count ?? 0;
  const catalog = useCatalogIndex(fetchProducts);
  return (
    <BagPanel
      title="Cart"
      count={plural(count, "item")}
      isEmpty={items.length === 0}
      empty={
        <>
          Your cart is empty.
          <br />
          Ask the {assistantName} for anything the store sells.
        </>
      }
      footer={
        <>
          <TotalRow label={count ? `Subtotal · ${plural(count, "item")}` : "Subtotal"} value={formatMoney(cart?.subtotal ?? 0, cart?.currency)} />
          <CheckoutAction />
          <CheckoutFootnote />
          {items.length ? (
            <div className="mt-2.5 flex justify-center">
              <AskLink label="Ask about this cart" prompt="Look over my cart: is anything missing or worth swapping?" />
            </div>
          ) : null}
        </>
      }
    >
      <ul className="divide-y divide-(--line)">
        {items.map((item) => {
          const product = asProduct(item, catalog);
          return (
            <li key={item.product_id} className="ac-reveal flex gap-3 py-3 first:pt-0">
              <ProductImage product={product} className="h-16 w-16 shrink-0 rounded-[10px] !text-3xl" />
              <div className="min-w-0 flex-1">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    {product.brand ? <div className="text-[10.5px] font-semibold uppercase tracking-[0.06em] text-(--ink-soft)">{product.brand}</div> : null}
                    <ProductTitle title={item.title} className="line-clamp-3 text-[13.5px] font-semibold leading-snug text-(--ink)" />
                    {optionValuesLabel(item) ? <div className="text-[11.5px] text-(--ink-soft)">{optionValuesLabel(item)}</div> : null}
                  </div>
                  <div className="shrink-0 text-right">
                    <div className="text-[14px] font-bold tabular-nums text-(--ink)">{formatMoney(item.line_total, cart?.currency)}</div>
                    {item.quantity > 1 ? <div className="text-[11px] text-(--ink-soft)">{formatMoney(item.price, cart?.currency)} each</div> : null}
                  </div>
                </div>
                <div className="mt-2 flex items-center gap-2.5">
                  {/* Every quantity change is a message, so the assistant makes the write and knows about it. */}
                  <Stepper
                    quantity={item.quantity}
                    itemTitle={lineName(item)}
                    onChange={(quantity) => ask(quantity < 1 ? `Remove the ${lineName(item)} from my cart.` : `Change the ${lineName(item)} quantity to ${quantity}.`)}
                  />
                  <RemoveLink itemTitle={lineName(item)} onClick={() => ask(`Remove the ${lineName(item)} from my cart.`)} />
                </div>
              </div>
            </li>
          );
        })}
      </ul>
    </BagPanel>
  );
}

/** The cart slides in from the right over the page; Escape and the scrim close it. */
export default function CartDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const panelRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    panelRef.current?.querySelector<HTMLElement>("button")?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      opener?.focus();
    };
  }, [open, onClose]);

  return (
    <>
      <div onClick={onClose} aria-hidden className={`fixed inset-0 z-40 bg-black/35 transition-opacity duration-300 ${open ? "opacity-100" : "pointer-events-none opacity-0"}`} />
      {/* Closed, the drawer is invisible: out of the tab order and the accessibility tree. */}
      <aside
        ref={panelRef}
        aria-label="Cart"
        className={`fixed inset-y-0 right-0 z-50 flex w-[min(92vw,400px)] flex-col border-l border-(--line) bg-(--card) ${
          open ? "visible translate-x-0 shadow-2xl [transition:transform_300ms]" : "invisible translate-x-full [transition:transform_300ms,visibility_0s_linear_300ms]"
        }`}
      >
        <CartLines />
      </aside>
    </>
  );
}
