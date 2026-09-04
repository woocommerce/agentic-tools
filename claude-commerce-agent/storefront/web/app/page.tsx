// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Adapted from Anthropic's retail storefront example (Apache-2.0): its home greeted a named
// shopper and showed featured picks; this one is the store's own front page.

"use client";

import { useRouter } from "next/navigation";
import { Notice, plural, useResource } from "web-shared";
import ProductTile from "@/components/ProductTile";
import { fetchProducts } from "@/lib/api";
import { useStore } from "@/lib/store";
import type { Brand } from "@/lib/types";

const FIRST_PROMPT = "What's in the catalog?";

/** The store's banner: its cover photo when it has one, otherwise its colours. */
function Hero({ brand }: { brand: Brand | null }) {
  const { ask, chat } = useStore();
  const tagline = brand?.tagline ?? brand?.slogan ?? null;
  const cover = brand?.cover_image_url ?? null;
  return (
    <section
      className="ac-reveal relative overflow-hidden rounded-2xl bg-(--brand) px-6 py-8 text-(--brand-contrast) shadow-(--shadow) sm:px-8 sm:py-10"
      style={cover ? { backgroundImage: `url(${cover})`, backgroundSize: "cover", backgroundPosition: "center" } : undefined}
    >
      {cover ? <div aria-hidden className="absolute inset-0 bg-black/45" /> : null}
      <div className="relative flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-4">
          {brand?.logo_url ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={brand.logo_url} alt="" className="h-14 w-14 shrink-0 rounded-xl bg-white/90 object-contain p-1.5" />
          ) : null}
          <div>
            <h1 className="text-[26px] font-bold leading-tight tracking-[-0.02em] sm:text-[30px]">{brand?.name ?? "Welcome"}</h1>
            {tagline ? <p className="mt-1 max-w-[52ch] text-[15px] leading-relaxed opacity-90">{tagline}</p> : null}
          </div>
        </div>
        <button
          type="button"
          onClick={() => ask(FIRST_PROMPT)}
          disabled={!chat.ready || chat.busy}
          className="shrink-0 rounded-(--radius) bg-white/95 px-4 py-2.5 text-[14px] font-semibold text-(--ink) shadow-(--shadow-sm) transition hover:bg-white disabled:opacity-60"
        >
          Ask the assistant
        </button>
      </div>
    </section>
  );
}

function GridSkeleton() {
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 xl:grid-cols-4" aria-hidden>
      {Array.from({ length: 8 }, (_, index) => (
        <div key={index} className="ac-skeleton h-[250px] rounded-xl" />
      ))}
    </div>
  );
}

export default function HomePage() {
  const { brand, chat, quickAdd, ask } = useStore();
  const router = useRouter();
  // The grid is what the host has seen of the catalog, so it re-reads once each reply settles.
  const { data: products, failed } = useResource(() => fetchProducts(), [chat.completed]);

  return (
    <div className="mx-auto flex w-full max-w-[1120px] flex-col gap-6 px-4 py-6 sm:px-6">
      <Hero brand={brand} />
      <section>
        <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h2 className="text-[17px] font-semibold tracking-[-0.01em] text-(--ink)">Products</h2>
          {products?.length ? <span className="text-[12.5px] text-(--ink-soft)">{plural(products.length, "product")} · open one, or tap + to add it</span> : null}
        </div>
        {products === null ? (
          failed ? (
            <Notice>Couldn&apos;t load the catalog. {chat.ready ? "The store may be offline; the assistant can still try a search." : "Check that the storefront API is running."}</Notice>
          ) : (
            <GridSkeleton />
          )
        ) : products.length === 0 ? (
          <div className="rounded-2xl border border-dashed border-(--line-strong) bg-(--card) p-8 text-center">
            <p className="text-[15px] font-semibold text-(--ink)">Nothing here yet</p>
            <p className="mx-auto mt-1 max-w-[46ch] text-[14px] leading-relaxed text-(--ink-soft)">
              The grid fills with what the assistant finds as you shop. Ask it what the store sells to get started.
            </p>
            <button type="button" onClick={() => ask(FIRST_PROMPT)} disabled={!chat.ready || chat.busy} className="btn-primary mt-4 inline-block disabled:opacity-60">
              {FIRST_PROMPT}
            </button>
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 xl:grid-cols-4">
            {products.map((product) => (
              <ProductTile key={product.product_id} product={product} fluid onAdd={quickAdd} onOpen={(item) => router.push(`/products/${encodeURIComponent(item.product_id)}`)} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
