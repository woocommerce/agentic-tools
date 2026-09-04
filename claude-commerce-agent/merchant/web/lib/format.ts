// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Adapted from Anthropic's retail merchant portal example (Apache-2.0).

/** Store-specific labels on top of web-shared's formatters. */

import { formatDayMonth, formatMoney, plural, type RecordRowData, titleCase } from "web-shared";
import { ORDER_STATUS } from "./kinds";
import type { RecentOrder, StoreKind } from "./types";

/** WooCommerce categories arrive as the store's own slugs or names; no fixed label table. */
export function formatCategoryLabel(category: string): string {
  if (category.includes(" ") || /[A-Z]/.test(category)) return category;
  return titleCase(category.replaceAll("-", "_"));
}

/** The sidebar's line under the store name: where the figures come from. */
export function storeDetail(kind: StoreKind | undefined, url: string | undefined): string {
  if (kind === "local") return "Local store";
  if (!url) return "Merchant workspace";
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

export function orderRows(orders: RecentOrder[], currency?: string): RecordRowData[] {
  return orders.map((order) => ({
    id: `#${order.order_id}`,
    detail: plural(order.items, "item"),
    sub: `${formatDayMonth(order.placed_at)} · ${formatMoney(order.total, currency)}`,
    status: ORDER_STATUS[order.status] ?? { label: order.status.replaceAll("_", " "), tone: "muted" },
  }));
}
