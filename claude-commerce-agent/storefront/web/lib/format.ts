// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Adapted from Anthropic's retail storefront example (Apache-2.0).

/** A product without a photo gets a tinted tile and a glyph, both chosen from its id and title. */

import { formatMoney } from "web-shared";
import type { Product } from "./types";

const TILE_CLASSES = [
  "bg-amber-100 text-amber-900",
  "bg-emerald-100 text-emerald-900",
  "bg-sky-100 text-sky-900",
  "bg-rose-100 text-rose-900",
  "bg-violet-100 text-violet-900",
  "bg-lime-100 text-lime-900",
  "bg-orange-100 text-orange-900",
  "bg-cyan-100 text-cyan-900",
];

function hash(text: string): number {
  let value = 0;
  for (let i = 0; i < text.length; i++) {
    value = (value * 31 + text.charCodeAt(i)) >>> 0;
  }
  return value;
}

/** Checked in order against the lowercased title; the first hit wins. */
const KEYWORD_GLYPHS: [string, string][] = [
  ["shirt", "👕"], ["tee", "👕"], ["hoodie", "🧥"], ["jacket", "🧥"], ["hat", "🧢"], ["cap", "🧢"],
  ["sock", "🧦"], ["shoe", "👟"], ["sneaker", "👟"], ["boot", "🥾"], ["bag", "👜"], ["belt", "👔"],
  ["mug", "☕"], ["coffee", "☕"], ["tea", "🍵"], ["bottle", "🧴"], ["candle", "🕯️"], ["soap", "🧼"],
  ["book", "📚"], ["notebook", "📓"], ["pen", "🖊️"], ["poster", "🖼️"], ["print", "🖼️"], ["album", "💿"],
  ["headphone", "🎧"], ["speaker", "🔊"], ["camera", "📷"], ["lamp", "💡"], ["light", "💡"], ["clock", "⏰"],
  ["plant", "🪴"], ["seed", "🌱"], ["tool", "🛠️"], ["hammer", "🔨"], ["drill", "🪛"], ["saw", "🪚"],
  ["knife", "🔪"], ["pan", "🍳"], ["pot", "🍲"], ["board", "🪵"], ["pillow", "🛏️"], ["blanket", "🛏️"],
  ["toy", "🧸"], ["puzzle", "🧩"], ["game", "🎲"], ["ball", "⚽"], ["bike", "🚲"], ["tent", "⛺"],
  ["dog", "🐕"], ["cat", "🐈"], ["pet", "🐾"], ["gift", "🎁"], ["card", "💳"], ["sticker", "✨"],
];

const FALLBACK_GLYPHS = ["🛍️", "📦", "🏷️", "✨"];

export function productGlyph(product: { title?: string; product_id?: string }): string {
  const title = (product.title ?? "").toLowerCase();
  for (const [keyword, glyph] of KEYWORD_GLYPHS) {
    if (title.includes(keyword)) return glyph;
  }
  return FALLBACK_GLYPHS[hash(product.product_id ?? title) % FALLBACK_GLYPHS.length];
}

export function productTileClass(productId: string): string {
  return TILE_CLASSES[hash(productId) % TILE_CLASSES.length];
}

/** "Was $49" when WooCommerce reports a regular price above the selling price; empty otherwise. */
export function wasPriceLabel(product: Pick<Product, "price" | "currency" | "attributes">): string {
  const regular = Number(product.attributes?.regular_price);
  if (!Number.isFinite(regular) || regular <= product.price) return "";
  return `Was ${formatMoney(regular, product.currency)}`;
}

/** The SKU when the store publishes one. */
export function skuLabel(product: Pick<Product, "attributes">): string {
  return product.attributes?.sku ? `SKU ${product.attributes.sku}` : "";
}
