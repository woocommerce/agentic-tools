// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Written for the WooCommerce storefront.

import type { Brand } from "./types";

/**
 * The theme's colours, when the host passed them on, become the page's `--brand` pair; without
 * them the defaults in globals.css stay in force.
 */
export function applyBrandColors(brand: Brand | null): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement.style;
  if (brand?.colors) {
    root.setProperty("--brand", brand.colors.background);
    root.setProperty("--brand-contrast", brand.colors.foreground);
  } else {
    root.removeProperty("--brand");
    root.removeProperty("--brand-contrast");
  }
}

/** One or two initials for the store mark when the site has no logo. */
export function brandInitials(name: string): string {
  const words = name.replace(/^https?:\/\//, "").split(/[\s.\-_/]+/).filter(Boolean);
  return words.slice(0, 2).map((word) => word[0]!.toUpperCase()).join("") || "W";
}

/** How the assistant is named in the rail and in the sheets: the store's own name, once known. */
export function assistantNameFor(brand: Brand | null): string {
  return brand ? `${brand.name} assistant` : "Shopping assistant";
}
