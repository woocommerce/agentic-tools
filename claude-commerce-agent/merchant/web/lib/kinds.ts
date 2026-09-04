// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Adapted from Anthropic's retail merchant portal example (Apache-2.0).

/** How each kind of store record shows: its label, icon, and tone. */

import type { KindStyle, Tone } from "web-shared";
import type { InventoryAlert, ListingStatus, OrderIssue } from "./types";

export const ISSUE_KINDS: Record<OrderIssue["kind"], KindStyle> = {
  delayed: { label: "Delayed", icon: "truck", tone: "warn" },
  buyer_message: { label: "Buyer message", icon: "message", tone: "info" },
  return_spike: { label: "Return spike", icon: "return", tone: "danger" },
};

/** An issue kind this build does not know still renders, as a generic alert. */
export const UNKNOWN_ISSUE: KindStyle = { label: "Issue", icon: "alert", tone: "warn" };

export function issueStyle(kind: string): KindStyle {
  return (ISSUE_KINDS as Record<string, KindStyle>)[kind] ?? UNKNOWN_ISSUE;
}

export const INVENTORY_KINDS: Record<InventoryAlert["kind"], KindStyle> = {
  low_stock: { label: "Low stock", icon: "low", tone: "warn" },
  slow_mover: { label: "Slow mover", icon: "clock", tone: "muted" },
};

export const LISTING_STATUS: Record<ListingStatus, { label: string; tone: Tone }> = {
  active: { label: "Active", tone: "ok" },
  paused: { label: "Paused", tone: "muted" },
  draft: { label: "Draft", tone: "info" },
  out_of_stock: { label: "Out of stock", tone: "danger" },
};

/**
 * Order statuses as the shared router reports them (the storefront view's buyer-side
 * statuses) plus WooCommerce's own, so a raw status still gets a label and tone.
 */
export const ORDER_STATUS: Record<string, { label: string; tone: Tone }> = {
  pending: { label: "Pending payment", tone: "muted" },
  on_hold: { label: "On hold", tone: "warn" },
  "on-hold": { label: "On hold", tone: "warn" },
  processing: { label: "Processing", tone: "muted" },
  shipped: { label: "Shipped", tone: "info" },
  out_for_delivery: { label: "Out for delivery", tone: "info" },
  completed: { label: "Completed", tone: "ok" },
  delivered: { label: "Delivered", tone: "ok" },
  delayed: { label: "Delayed", tone: "warn" },
  cancelled: { label: "Cancelled", tone: "muted" },
  failed: { label: "Failed", tone: "danger" },
  return_initiated: { label: "Return requested", tone: "violet" },
  refunded: { label: "Refunded", tone: "ok" },
};
