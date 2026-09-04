// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Adapted from Anthropic's retail storefront example (Apache-2.0).

import type { GuidePayload } from "@/lib/types";
import ProductTile from "../ProductTile";

/** Advice in sections, with the products it mentions in a strip underneath. */
export default function GuideCard({ payload }: { payload: GuidePayload }) {
  return (
    <section className="rounded-2xl border border-(--line) bg-(--card) p-4 shadow-(--shadow-sm)">
      <h3 className="text-[15px] font-semibold text-(--ink)">{payload.title}</h3>
      <div className="mt-2 space-y-3">
        {(payload.sections ?? []).map((section, index) => (
          <div key={index}>
            <div className="text-[13px] font-bold uppercase tracking-wide text-(--ink)">{section.heading}</div>
            <p className="mt-0.5 text-[15px] leading-relaxed text-(--ink)">{section.body}</p>
          </div>
        ))}
      </div>
      {payload.related_products?.length ? (
        <div className="panel-scroll mt-3 flex gap-3 overflow-x-auto border-t border-(--line) pt-3">
          {payload.related_products.map((product) => (
            <ProductTile key={product.product_id} product={product} compact />
          ))}
        </div>
      ) : null}
      {payload.sources?.length ? (
        <p className="mt-3 break-all text-[11px] text-(--ink-soft)/80">Sources: {payload.sources.join(" · ")}</p>
      ) : null}
    </section>
  );
}
