// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Adapted from Anthropic's retail storefront example (Apache-2.0).

"use client";

import { useState } from "react";
import { hasOptions, optionSummary, optionValuesLabel, priceLabel, useStoreFrame } from "web-shared";
import { flyToCart } from "@/lib/flight";
import { productGlyph, productTileClass, wasPriceLabel } from "@/lib/format";
import type { Product } from "@/lib/types";

export type AddHandler = (product: Product) => boolean | void | Promise<boolean | void>;

/** A trailing bracketed part such as "(Pack of 3)" stays on one line so a clamp cuts before it. */
export function ProductTitle({ title, className = "" }: { title: string; className?: string }) {
  const match = /^(.*\S)\s+(\([^()]+\))$/.exec(title);
  return (
    <div className={className} title={title}>
      {match ? (
        <>
          {match[1]} <span className="whitespace-nowrap">{match[2]}</span>
        </>
      ) : (
        title
      )}
    </div>
  );
}

export function ProductImage({ product, className = "" }: { product: Product; className?: string }) {
  if (product.image_url) {
    // eslint-disable-next-line @next/next/no-img-element
    return <img src={product.image_url} alt={product.title} className={`object-cover ${className}`} />;
  }
  return (
    <div className={`flex items-center justify-center text-5xl ${productTileClass(product.product_id)} ${className}`} aria-hidden>
      {productGlyph(product)}
    </div>
  );
}

export function Rating({ rating, count }: { rating?: number | null; count?: number | null }) {
  if (rating == null) return null;
  return (
    <span className="whitespace-nowrap text-[13px] text-(--ink-soft)">
      <span className="text-(--star)">★</span> {rating.toFixed(1)}
      {count ? <span className="text-[11px] text-(--ink-soft)/80"> ({count.toLocaleString()})</span> : null}
    </span>
  );
}

/** The old price, struck through, on a discounted product. */
export function WasPrice({ product, className = "" }: { product: Product; className?: string }) {
  const label = wasPriceLabel(product);
  if (!label) return null;
  return <span className={`text-[11px] text-(--ink-soft) line-through ${className}`}>{label.replace(/^Was /, "")}</span>;
}

function StockBadge({ product, className = "" }: { product: Product; className?: string }) {
  if (product.in_stock !== false) return null;
  return (
    <span className={`rounded-full bg-(--ink)/85 px-2 py-0.5 text-[11px] font-medium text-(--surface) ${className}`}>
      Out of stock
    </span>
  );
}

/** A variation's chosen values, or the options a variable product still leaves open. */
function optionText(product: Product): string {
  return optionValuesLabel(product) || optionSummary(product);
}

export function OptionLine({ product, className = "" }: { product: Product; className?: string }) {
  const text = optionText(product);
  if (!text) return null;
  return <div className={`truncate text-[11px] text-(--ink-soft) ${className}`}>{text}</div>;
}

/**
 * The round add button over a card's image. A variable product is not added from here: the
 * button asks the assistant instead, which settles the variation with the shopper. `onAdd`
 * resolving `false` means the host refused the write.
 */
export function AddButton({ product, onAdd }: { product: Product; onAdd: AddHandler }) {
  const [phase, setPhase] = useState<"idle" | "busy" | "done" | "error">("idle");
  const { ask } = useStoreFrame();
  if (hasOptions(product)) {
    return (
      <button
        type="button"
        onClick={(event) => {
          event.stopPropagation();
          ask(`Add the ${product.title} (${product.product_id}) to my cart.`);
        }}
        aria-label={`Choose options for ${product.title}`}
        className="pointer-events-auto absolute bottom-2 right-2 flex h-8 w-8 items-center justify-center rounded-full bg-(--ink) text-lg font-semibold leading-none text-(--surface) shadow-(--shadow-sm) transition-all hover:scale-105"
      >
        +
      </button>
    );
  }
  return (
    <button
      type="button"
      onClick={async (event) => {
        event.stopPropagation();
        if (phase !== "idle") return;
        const source = event.currentTarget.parentElement ?? event.currentTarget;
        setPhase("busy");
        const added = (await onAdd(product)) !== false;
        setPhase(added ? "done" : "error");
        // The marker flies only once the host has said yes.
        if (added) flyToCart(product, source);
        window.setTimeout(() => setPhase("idle"), added ? 1200 : 1600);
      }}
      aria-label={`Add ${product.title} to cart`}
      className={`pointer-events-auto absolute bottom-2 right-2 flex h-8 w-8 items-center justify-center rounded-full text-lg font-semibold leading-none text-(--surface) shadow-(--shadow-sm) transition-all hover:scale-105 ${
        phase === "done" ? "bg-(--ok)" : phase === "error" ? "bg-(--warn)" : "bg-(--ink)"
      } ${phase === "busy" ? "animate-pulse" : ""}`}
    >
      {phase === "done" ? "✓" : phase === "error" ? "!" : "+"}
    </button>
  );
}

export default function ProductTile({
  product,
  compact = false,
  fluid = false,
  selected = false,
  onAdd,
  onOpen,
}: {
  product: Product;
  compact?: boolean;
  /** Takes the width of its grid cell instead of the carousel's fixed width. */
  fluid?: boolean;
  selected?: boolean;
  onAdd?: AddHandler;
  onOpen?: (product: Product) => void;
}) {
  const clickable = Boolean(onOpen);
  const imageHeight = compact ? "h-16" : fluid ? "h-40" : "h-24";
  const was = compact ? "" : wasPriceLabel(product);
  return (
    <div
      className={`relative flex shrink-0 flex-col overflow-hidden rounded-xl border bg-(--card) shadow-(--shadow-sm) transition-[box-shadow,border-color] duration-200 hover:shadow-md ${
        fluid ? "w-full" : compact ? "w-36" : "w-48"
      } ${selected ? "border-(--ink)" : "border-(--line)"}`}
    >
      <div
        onClick={clickable ? () => onOpen?.(product) : undefined}
        onKeyDown={clickable ? (event) => event.key === "Enter" && onOpen?.(product) : undefined}
        role={clickable ? "button" : undefined}
        tabIndex={clickable ? 0 : undefined}
        className={`flex flex-1 flex-col rounded-xl focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-(--accent) ${clickable ? "cursor-pointer" : ""}`}
      >
        <div className="relative">
          <ProductImage product={product} className={`w-full ${imageHeight}`} />
          <StockBadge product={product} className="absolute right-1.5 top-1.5" />
          {was && product.in_stock !== false ? (
            <span className="absolute left-1.5 top-1.5 rounded-full bg-(--danger) px-2 py-0.5 text-[11px] font-semibold text-white">Sale</span>
          ) : null}
        </div>
        <div className="flex flex-1 flex-col gap-0.5 p-2.5">
          {product.brand ? <div className="text-[11px] uppercase tracking-wide text-(--ink-soft)/80">{product.brand}</div> : null}
          <ProductTitle title={product.title} className={`line-clamp-2 text-[13px] font-medium leading-snug ${compact ? "" : "h-9"}`} />
          {compact ? null : (
            /* A fixed-height line keeps neighbouring cards' price rows level. */
            <div className="h-[18px] pt-0.5 leading-4">
              {optionText(product) ? (
                <OptionLine product={product} />
              ) : product.short_description ? (
                <div className="truncate text-[11px] text-(--ink-soft)">{product.short_description}</div>
              ) : null}
            </div>
          )}
          <div className="mt-auto flex items-center justify-between gap-1 pt-0.5">
            <span className="flex items-baseline gap-1.5">
              <span className="text-sm font-semibold">{priceLabel(product)}</span>
              {compact ? null : <WasPrice product={product} />}
            </span>
            <Rating rating={product.rating} count={compact ? undefined : product.review_count} />
          </div>
        </div>
      </div>
      {onAdd && product.in_stock !== false ? (
        // Layered over the image but outside the clickable area, so one control does not sit inside another.
        <div className={`pointer-events-none absolute inset-x-0 top-0 ${imageHeight}`}>
          <AddButton product={product} onAdd={onAdd} />
        </div>
      ) : null}
    </div>
  );
}

export function ProductRow({ product, onAdd }: { product: Product; onAdd?: AddHandler }) {
  return (
    <div className="flex w-full items-center gap-3 rounded-xl border border-(--line) bg-(--card) p-2 shadow-(--shadow-sm) transition-shadow hover:shadow-md">
      <div className="relative shrink-0">
        <ProductImage product={product} className={`h-14 w-16 rounded-lg ${product.in_stock === false ? "opacity-50" : ""}`} />
        {onAdd && product.in_stock !== false ? <AddButton product={product} onAdd={onAdd} /> : null}
      </div>
      <div className="min-w-0 flex-1">
        {product.brand ? <div className="text-[11px] uppercase tracking-wide text-(--ink-soft)/80">{product.brand}</div> : null}
        <ProductTitle title={product.title} className="line-clamp-1 text-[13px] font-medium leading-snug" />
        <OptionLine product={product} />
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold">{priceLabel(product)}</span>
          <WasPrice product={product} />
          <Rating rating={product.rating} />
          <StockBadge product={product} />
        </div>
      </div>
    </div>
  );
}
