// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Written for the WooCommerce storefront from the retail example's inline product detail; a
// variable product's variations are picked here before the add.

"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { AskLink, formatMoney, Notice, optionValuesLabel, priceLabel, Skeleton } from "web-shared";
import { ProductImage, Rating, WasPrice } from "@/components/ProductTile";
import { fetchProduct } from "@/lib/api";
import { flyToCart } from "@/lib/flight";
import { skuLabel } from "@/lib/format";
import { useStore } from "@/lib/store";
import type { Product, ProductDetails } from "@/lib/types";

const MAX_QUANTITY = 10;

/** One button per variation; the label is its option values, or its title when it has none. */
function VariationPicker({ variants, selected, onSelect }: { variants: Product[]; selected: Product | null; onSelect: (variant: Product) => void }) {
  const pricesDiffer = variants.some((variant) => variant.price !== variants[0]?.price);
  return (
    <fieldset className="mt-5">
      <legend className="text-[13px] font-semibold text-(--ink)">Choose a variation</legend>
      <div className="mt-2 flex flex-wrap gap-2" role="radiogroup">
        {variants.map((variant) => {
          const active = selected?.product_id === variant.product_id;
          const available = variant.in_stock !== false;
          return (
            <button
              key={variant.product_id}
              type="button"
              role="radio"
              aria-checked={active}
              disabled={!available}
              onClick={() => onSelect(variant)}
              className={`rounded-full border px-3 py-1.5 text-[13px] transition-colors disabled:cursor-not-allowed disabled:text-(--ink-soft)/70 disabled:line-through ${
                active ? "border-(--ink) bg-(--ink) text-(--surface)" : "border-(--line-strong) bg-(--card) text-(--ink) hover:border-(--ink)"
              }`}
            >
              {optionValuesLabel(variant) || variant.title}
              {pricesDiffer ? <span className={active ? "opacity-80" : "text-(--ink-soft)"}> · {formatMoney(variant.price, variant.currency)}</span> : null}
            </button>
          );
        })}
      </div>
    </fieldset>
  );
}

export default function ProductPage() {
  const { id } = useParams<{ id: string }>();
  const { addToCart, ask, openCart, brand } = useStore();
  const [details, setDetails] = useState<ProductDetails | null | undefined>(undefined);
  const [variant, setVariant] = useState<Product | null>(null);
  const [quantity, setQuantity] = useState(1);
  const [phase, setPhase] = useState<"idle" | "busy" | "added">("idle");
  const [refusal, setRefusal] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    setDetails(undefined);
    setVariant(null);
    setRefusal(null);
    void fetchProduct(id).then((loaded) => {
      if (!mounted) return;
      setDetails(loaded);
      // A single in-stock variation needs no choosing.
      const stocked = (loaded?.variants ?? []).filter((item) => item.in_stock !== false);
      if (stocked.length === 1) setVariant(stocked[0]);
    });
    return () => {
      mounted = false;
    };
  }, [id]);

  if (details === undefined) {
    return (
      <div className="mx-auto w-full max-w-[1000px] px-4 py-6 sm:px-6">
        <Skeleton className="h-[420px]" />
      </div>
    );
  }
  if (details === null) {
    return (
      <div className="mx-auto w-full max-w-[1000px] px-4 py-6 sm:px-6">
        <Notice>
          That product isn&apos;t available. <Link href="/" className="font-semibold text-(--accent-ink) hover:text-(--accent)">Back to the store</Link>.
        </Notice>
      </div>
    );
  }

  const variants = details.variants ?? [];
  const isVariable = variants.length > 0;
  // What the page shows and what the cart gets: the chosen variation, else the product itself.
  const shown: Product = variant ? { ...details, ...variant, image_url: variant.image_url ?? details.image_url } : details;
  const canAdd = shown.in_stock !== false && (!isVariable || variant !== null);
  const specs = details.specs ?? {};

  const add = async (event: React.MouseEvent<HTMLButtonElement>) => {
    if (!canAdd || phase !== "idle") return;
    const source = event.currentTarget;
    setPhase("busy");
    setRefusal(null);
    const result = await addToCart(shown.product_id, quantity);
    if (result.ok) {
      setPhase("added");
      flyToCart(shown, source);
      window.setTimeout(() => setPhase("idle"), 1500);
    } else {
      setPhase("idle");
      setRefusal(result.reason);
    }
  };

  return (
    <div className="mx-auto w-full max-w-[1000px] px-4 py-6 sm:px-6">
      <Link href="/" className="inline-flex items-center gap-1 text-[13px] font-semibold text-(--ink-2) hover:text-(--ink)">
        <span aria-hidden>←</span> All products
      </Link>
      <div className="mt-4 grid gap-6 md:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
        <div className="overflow-hidden rounded-2xl border border-(--line) bg-(--card) shadow-(--shadow-sm)">
          <ProductImage key={shown.image_url ?? shown.product_id} product={shown} className="ac-reveal aspect-square w-full !text-8xl" />
        </div>
        <div>
          {details.brand ? <div className="text-[12px] uppercase tracking-wide text-(--ink-soft)">{details.brand}</div> : null}
          <h1 className="text-[26px] font-bold leading-tight tracking-[-0.02em] text-(--ink)">{details.title}</h1>
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <span className="text-[22px] font-bold tabular-nums text-(--ink)">{variant ? formatMoney(variant.price, variant.currency) : priceLabel(details)}</span>
            <WasPrice product={shown} className="text-[14px]" />
            <Rating rating={details.rating} count={details.review_count} />
            {shown.in_stock === false ? <span className="rounded-full bg-(--ink)/85 px-2.5 py-0.5 text-[12px] font-medium text-(--surface)">Out of stock</span> : null}
          </div>
          {skuLabel(shown) ? <div className="mt-1 text-[12px] text-(--ink-soft)">{skuLabel(shown)}</div> : null}
          {details.short_description ? <p className="mt-4 text-[15px] leading-relaxed text-(--ink-2)">{details.short_description}</p> : null}

          {isVariable ? <VariationPicker variants={variants} selected={variant} onSelect={setVariant} /> : null}

          <div className="mt-6 flex flex-wrap items-center gap-3">
            <div className="flex items-center rounded-full border border-(--line-strong) bg-(--card)">
              <button type="button" onClick={() => setQuantity((value) => Math.max(1, value - 1))} disabled={quantity <= 1} aria-label="Fewer" className="px-3 py-1.5 text-(--ink-soft) hover:text-(--ink) disabled:opacity-40">
                −
              </button>
              <span className="min-w-7 text-center text-[14px] font-semibold tabular-nums text-(--ink)">{quantity}</span>
              <button type="button" onClick={() => setQuantity((value) => Math.min(MAX_QUANTITY, value + 1))} disabled={quantity >= MAX_QUANTITY} aria-label="More" className="px-3 py-1.5 text-(--ink-soft) hover:text-(--ink) disabled:opacity-40">
                +
              </button>
            </div>
            <button type="button" onClick={add} disabled={!canAdd || phase === "busy"} className={`btn-primary min-w-[160px] ${phase === "busy" ? "animate-pulse" : ""}`}>
              {phase === "added" ? "Added ✓" : shown.in_stock === false ? "Out of stock" : isVariable && !variant ? "Choose a variation" : "Add to cart"}
            </button>
            {phase === "added" ? (
              <button type="button" onClick={openCart} className="text-[13px] font-semibold text-(--accent-ink) hover:text-(--accent)">
                View cart
              </button>
            ) : null}
          </div>
          {refusal ? (
            <div role="alert" className="mt-3 rounded-xl border border-(--warn)/40 bg-(--warn-soft) px-3.5 py-2.5 text-[13px] leading-snug text-(--ink)">
              {refusal}
              <div className="mt-1.5">
                <AskLink label="Ask the assistant about it" prompt={`Tell me about the ${details.title} (${details.product_id}).`} />
              </div>
            </div>
          ) : (
            <div className="mt-3">
              <AskLink label={`Ask the ${brand?.name ?? "store"} assistant about this`} prompt={`Tell me about the ${details.title} (${details.product_id}).`} />
            </div>
          )}

          {details.long_description ? (
            <section className="mt-8">
              <h2 className="text-[15px] font-semibold text-(--ink)">About this product</h2>
              <p className="mt-1.5 whitespace-pre-line text-[14.5px] leading-relaxed text-(--ink-2)">{details.long_description}</p>
            </section>
          ) : null}
          {Object.keys(specs).length ? (
            <section className="mt-6">
              <h2 className="text-[15px] font-semibold text-(--ink)">Details</h2>
              <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
                {Object.entries(specs).map(([key, value]) => (
                  <div key={key} className="text-[13px]">
                    <dt className="font-semibold capitalize text-(--ink-soft)">{key.replaceAll("_", " ")}</dt>
                    <dd className="text-(--ink)">{value}</dd>
                  </div>
                ))}
              </dl>
            </section>
          ) : null}
          {details.review_highlights?.length ? (
            <section className="mt-6">
              <h2 className="text-[15px] font-semibold text-(--ink)">What reviewers say</h2>
              <div className="mt-2 space-y-1.5">
                {details.review_highlights.slice(0, 3).map((highlight) => (
                  <p key={highlight} className="text-[13.5px] italic leading-snug text-(--ink-soft)">
                    “{highlight}”
                  </p>
                ))}
              </div>
            </section>
          ) : null}
          <p className="mt-8 text-[12px] leading-relaxed text-(--ink-soft)">
            The add button can only add products the assistant has shown in this conversation; ask about one first if the store turns it down.
          </p>
        </div>
      </div>
    </div>
  );
}
