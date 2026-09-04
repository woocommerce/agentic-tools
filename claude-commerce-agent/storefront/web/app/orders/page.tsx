// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Adapted from Anthropic's retail storefront example (Apache-2.0).

"use client";

import { type Order, ORDER_NOUNS, OrdersView, useCatalogIndex, useResource } from "web-shared";
import { ProductImage } from "@/components/ProductTile";
import { api, fetchProducts } from "@/lib/api";
import { useStore } from "@/lib/store";

/** The first line's photo, or a glyph tile when the catalog has none for it. */
function OrderThumb({ order }: { order: Order }) {
  const catalog = useCatalogIndex(fetchProducts);
  const line = order.items[0];
  const product = catalog[line?.product_id ?? ""] ?? { product_id: line?.product_id ?? order.order_id, title: line?.title ?? "", price: 0 };
  return <ProductImage product={product} className="h-[42px] w-[42px] shrink-0 rounded-[9px] !text-xl" />;
}

export default function OrdersPage() {
  const { sessionId, chat } = useStore();
  // A reply may have placed or changed an order, so the list re-reads once each one settles.
  const { data: orders, failed } = useResource(sessionId ? () => api.fetchOrders() : null, [sessionId, chat.completed]);
  return (
    <div className="mx-auto flex w-full max-w-[808px] flex-col gap-4 px-4 py-6 sm:px-6">
      <OrdersView
        orders={orders}
        failed={failed}
        nouns={ORDER_NOUNS}
        subtitle="Orders placed from this browser's cart, once the store confirms them. Ask the assistant about any of them."
        thumb={(order) => <OrderThumb order={order} />}
      />
    </div>
  );
}
