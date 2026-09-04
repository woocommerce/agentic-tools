// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Adapted from Anthropic's retail storefront example (Apache-2.0).

"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { formatMoney, optionValuesLabel, priceLabel, useStoreFrame } from "web-shared";
import { fetchProduct } from "@/lib/api";
import { skuLabel } from "@/lib/format";
import type { Product, ProductDetails, ProductsPayload } from "@/lib/types";
import ProductTile, { AddButton, type AddHandler, OptionLine, ProductImage, Rating, WasPrice } from "../ProductTile";

/** A variable product's variations; picking one hands the add to the assistant, which knows the cart. */
function VariantList({ family, variants }: { family: Product; variants: Product[] }) {
  const { ask } = useStoreFrame();
  const pricesDiffer = variants.some((variant) => variant.price !== variants[0]?.price);
  return (
    <ul className="mt-2 flex flex-wrap gap-1.5" aria-label="Variations">
      {variants.map((variant) => {
        const label = optionValuesLabel(variant) || variant.title;
        const available = variant.in_stock !== false;
        return (
          <li key={variant.product_id}>
            <button
              type="button"
              disabled={!available}
              onClick={() => ask(`Add the ${family.title} in ${label} (${variant.product_id}) to my cart.`)}
              className="rounded-full border border-(--line) bg-(--card) px-2.5 py-1 text-[12px] text-(--ink) transition-colors hover:border-(--ink) disabled:cursor-not-allowed disabled:text-(--ink-soft)/70 disabled:line-through"
            >
              {label}
              {pricesDiffer ? <span className="text-(--ink-soft)"> · {formatMoney(variant.price, variant.currency)}</span> : null}
            </button>
          </li>
        );
      })}
    </ul>
  );
}

/** The panel that unfolds under the strip when a card is opened. */
function ProductDetail({ product, reason, onAdd, onClose }: { product: Product; reason?: string | null; onAdd?: AddHandler; onClose: () => void }) {
  const [details, setDetails] = useState<ProductDetails | null>(null);
  useEffect(() => {
    let mounted = true;
    void fetchProduct(product.product_id).then((value) => {
      if (mounted) setDetails(value);
    });
    return () => {
      mounted = false;
    };
  }, [product.product_id]);

  const full = details ?? product;
  const specs = details?.specs ?? {};
  return (
    <div className="ac-reveal mb-1 mt-3 rounded-xl border border-(--line) bg-(--well)/40 p-3">
      <div className="flex items-start gap-3">
        <div className="relative shrink-0">
          <ProductImage product={full} className="h-24 w-28 rounded-lg" />
          {onAdd && full.in_stock !== false ? <AddButton product={full} onAdd={onAdd} /> : null}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2">
            <div>
              {full.brand ? <div className="text-[11px] uppercase tracking-wide text-(--ink-soft)/80">{full.brand}</div> : null}
              <div className="text-sm font-semibold leading-snug">{full.title}</div>
              <OptionLine product={full} />
              <div className="mt-0.5 flex items-center gap-2">
                <span className="text-sm font-bold">{priceLabel(full)}</span>
                <WasPrice product={full} />
                <Rating rating={full.rating} count={full.review_count} />
                {full.in_stock === false ? <span className="rounded-full bg-(--ink)/85 px-2 py-0.5 text-[11px] font-medium text-(--surface)">Out of stock</span> : null}
              </div>
              {skuLabel(full) ? <div className="mt-0.5 text-[11px] text-(--ink-soft)">{skuLabel(full)}</div> : null}
            </div>
            <button onClick={onClose} aria-label="Close details" className="shrink-0 rounded-md px-1.5 text-base leading-none text-(--ink-soft) hover:text-(--ink)">
              ×
            </button>
          </div>
        </div>
      </div>

      {reason ? <p className="mt-2 text-[13px] leading-snug text-(--ink)">{reason}</p> : null}
      {details === null ? (
        <p className="mt-2 animate-pulse text-[13px] text-(--ink-soft)">Loading details…</p>
      ) : (
        <div className="ac-reveal">
          {details.long_description ? <p className="mt-2 text-[13px] leading-relaxed text-(--ink)">{details.long_description}</p> : null}
          {details.variants?.length ? <VariantList family={details} variants={details.variants} /> : null}
          {Object.keys(specs).length ? (
            <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-3">
              {Object.entries(specs).map(([key, value]) => (
                <div key={key} className="text-[13px]">
                  <dt className="font-semibold capitalize text-(--ink-soft)">{key.replaceAll("_", " ")}</dt>
                  <dd className="text-(--ink)">{value}</dd>
                </div>
              ))}
            </dl>
          ) : null}
          {details.review_highlights?.length ? (
            <div className="mt-2 space-y-1">
              {details.review_highlights.slice(0, 3).map((highlight) => (
                <p key={highlight} className="text-[13px] italic leading-snug text-(--ink-soft)">
                  “{highlight}”
                </p>
              ))}
            </div>
          ) : null}
        </div>
      )}
      <Link href={`/products/${encodeURIComponent(product.product_id)}`} className="mt-3 inline-flex items-center gap-1 text-[12.5px] font-semibold text-(--accent-ink) hover:text-(--accent)">
        Open the product page <span aria-hidden>→</span>
      </Link>
    </div>
  );
}

export default function ProductCarousel({ payload, onAdd, partial }: { payload: ProductsPayload; onAdd?: AddHandler; partial?: boolean }) {
  const layout = payload.layout ?? "carousel";
  const items = payload.items ?? [];
  const [expandedId, setExpandedId] = useState<string | null>(null);
  // The last opened product stays mounted while the panel folds, so closing animates too.
  const [renderedId, setRenderedId] = useState<string | null>(null);
  const collapseRef = useRef<HTMLDivElement>(null);

  const scrollerRef = useRef<HTMLDivElement>(null);
  const [overflow, setOverflow] = useState({ left: false, right: false });
  const syncOverflow = useCallback(() => {
    const node = scrollerRef.current;
    if (!node) return;
    setOverflow({ left: node.scrollLeft > 4, right: node.scrollLeft + node.clientWidth < node.scrollWidth - 4 });
  }, []);
  useEffect(() => {
    syncOverflow();
    const node = scrollerRef.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(syncOverflow);
    observer.observe(node);
    return () => observer.disconnect();
  }, [syncOverflow, items.length, partial]);
  const nudge = (direction: 1 | -1) => {
    const node = scrollerRef.current;
    node?.scrollBy({ left: direction * (node.clientWidth - 80), behavior: "smooth" });
  };

  useEffect(() => {
    if (expandedId) {
      setRenderedId(expandedId);
      // Scroll the opening panel into view once it has grown.
      const timer = window.setTimeout(() => collapseRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }), 180);
      return () => window.clearTimeout(timer);
    }
  }, [expandedId]);

  const rendered = items.find(({ product }) => product.product_id === renderedId);
  const open = expandedId != null && expandedId === renderedId;
  const toggle = (product: Product) => setExpandedId((current) => (current === product.product_id ? null : product.product_id));

  return (
    <section className="rounded-2xl border border-(--line) bg-(--card) p-3 shadow-(--shadow-sm)">
      {payload.title ? <h3 className="mb-3 text-[15px] font-semibold text-(--ink)">{payload.title}</h3> : null}
      <div className="relative">
        <div
          ref={scrollerRef}
          onScroll={layout === "carousel" ? syncOverflow : undefined}
          className={layout === "grid" ? "grid grid-cols-2 gap-3 sm:grid-cols-3" : layout === "list" ? "flex flex-col gap-3" : "panel-scroll flex gap-3 overflow-x-auto pb-1"}
        >
          {items.map(({ product }) => (
            <div key={product.product_id} className="ac-reveal shrink-0">
              <ProductTile product={product} onAdd={onAdd} onOpen={toggle} selected={product.product_id === expandedId} fluid={layout === "grid"} />
            </div>
          ))}
          {partial ? <div className="ac-skeleton h-[150px] w-48 shrink-0 rounded-xl" /> : null}
        </div>
        {overflow.left ? (
          <>
            <div aria-hidden className="pointer-events-none absolute inset-y-0 left-0 w-10 bg-linear-to-r from-(--card) to-transparent" />
            <button
              onClick={() => nudge(-1)}
              aria-label="Show previous products"
              className="absolute left-0 top-1/2 -translate-y-1/2 rounded-full border border-(--line) bg-(--card) px-2 py-1 text-sm text-(--ink) shadow-md transition hover:border-(--accent)"
            >
              ‹
            </button>
          </>
        ) : null}
        {overflow.right ? (
          <>
            <div aria-hidden className="pointer-events-none absolute inset-y-0 right-0 w-10 bg-linear-to-l from-(--card) to-transparent" />
            <button
              onClick={() => nudge(1)}
              aria-label="Show more products"
              className="absolute right-0 top-1/2 -translate-y-1/2 rounded-full border border-(--line) bg-(--card) px-2 py-1 text-sm text-(--ink) shadow-md transition hover:border-(--accent)"
            >
              ›
            </button>
          </>
        ) : null}
      </div>
      <div
        ref={collapseRef}
        className={`ac-collapse ${open ? "ac-collapse-open" : ""}`}
        onTransitionEnd={(event) => {
          if (event.propertyName === "grid-template-rows" && !expandedId) setRenderedId(null);
        }}
        aria-hidden={!open}
      >
        <div className="ac-collapse-inner">
          {rendered ? <ProductDetail key={rendered.product.product_id} product={rendered.product} reason={rendered.reason} onAdd={onAdd} onClose={() => setExpandedId(null)} /> : null}
        </div>
      </div>
    </section>
  );
}
