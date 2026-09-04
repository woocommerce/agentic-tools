// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Written for the WooCommerce storefront from the retail example's Chat and the shared pieces:
// the conversation lives beside the pages instead of being the page.

"use client";

import { ActivityLine, type AssistantChatItem, Chat, Icon, IconButton, type Starter, Starters } from "web-shared";
import { Composer } from "web-shared/Composer";
import { useStore } from "@/lib/store";
import GenerativeBlock from "./generative";

const STARTERS: Starter[] = [
  { icon: "search", prompt: "What's in the catalog?" },
  { icon: "tag", prompt: "Find me a gift under $50." },
  { icon: "chart", prompt: "Compare your two most popular products." },
  { icon: "return", prompt: "What's your returns policy?" },
];

/** Placeholder cards where a product strip will land while a search is running. */
function Pending({ item }: { item: AssistantChatItem }) {
  const searching = item.tools.includes("search_products") && !item.segments.some((segment) => segment.type === "ui");
  if (!searching) return <ActivityLine item={item} />;
  return (
    <section role="status" className="rounded-2xl border border-(--line) bg-(--card) p-3 shadow-(--shadow-sm)">
      <div className="mb-3 animate-pulse text-[15px] text-(--ink-soft)">{item.activity ?? "Searching the store…"}</div>
      <div className="flex gap-3 overflow-hidden pb-1">
        {[0, 1, 2].map((slot) => (
          <div key={slot} className="ac-skeleton h-[150px] w-48 shrink-0 rounded-xl" />
        ))}
      </div>
    </section>
  );
}

function Welcome() {
  const { brand, assistantName } = useStore();
  return (
    <div className="flex flex-col gap-4 pt-1">
      <div className="ac-reveal">
        <h2 className="text-[20px] font-semibold leading-tight tracking-[-0.02em] text-(--ink)">Hello, I&apos;m the {assistantName}.</h2>
        <p className="mt-2 text-[14.5px] leading-relaxed text-(--ink-2)">
          I know what {brand?.name ?? "the store"} sells, its policies, and your cart. Ask for a product, a comparison, or help choosing, and I&apos;ll show what I find here.
        </p>
      </div>
      <Starters items={STARTERS} />
    </div>
  );
}

/**
 * The conversation beside the pages: a column from `lg`, a drawer below it. Cards the assistant
 * streams render here through the generative registry.
 */
export default function AssistantRail({ open, onClose, onOpenActivity }: { open: boolean; onClose: () => void; onOpenActivity: () => void }) {
  const { chat, assistantName, quickAdd } = useStore();
  return (
    <>
      <div onClick={onClose} aria-hidden className={`fixed inset-0 z-30 bg-black/35 transition-opacity duration-300 lg:hidden ${open ? "opacity-100" : "pointer-events-none opacity-0"}`} />
      <aside
        aria-label={assistantName}
        className={`fixed inset-y-0 right-0 z-40 flex w-[min(94vw,420px)] flex-col border-l border-(--line) bg-(--card) lg:visible lg:static lg:z-auto lg:w-[400px] lg:shrink-0 lg:translate-x-0 lg:shadow-none lg:transition-none xl:w-[440px] ${
          open ? "visible translate-x-0 shadow-2xl [transition:transform_300ms]" : "invisible translate-x-full [transition:transform_300ms,visibility_0s_linear_300ms]"
        }`}
      >
        <div className="flex items-center gap-2.5 border-b border-(--line) py-3 pl-4 pr-2.5">
          <span className="grid h-[30px] w-[30px] shrink-0 place-items-center rounded-[10px] bg-(--accent) text-(--on-accent)" aria-hidden>
            <Icon name="spark" size={15} />
          </span>
          <div className="min-w-0 flex-1">
            <div className="truncate text-[14px] font-semibold leading-tight text-(--ink)">{assistantName}</div>
            <div className="truncate text-[11.5px] text-(--ink-soft)">{chat.streaming ? "Working…" : "Ask about anything in the store"}</div>
          </div>
          <button
            type="button"
            onClick={onOpenActivity}
            className="flex items-center gap-1.5 rounded-full border border-(--line) bg-(--card) px-3 py-1 text-[12.5px] font-semibold text-(--ink) transition hover:border-(--accent)"
          >
            {chat.streaming ? <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-(--accent)" aria-hidden /> : null}
            Activity
            {chat.newMemoryKeys.size ? (
              <span className="rounded-full bg-(--accent-soft) px-1.5 py-0.5 text-[11px] font-bold text-(--ink)" title="Facts saved this session">
                {chat.newMemoryKeys.size}
              </span>
            ) : null}
          </button>
          <IconButton icon="x" label="Hide assistant" onClick={onClose} className="lg:hidden" />
        </div>
        <div className="min-h-0 flex-1">
          <Chat
            chat={chat}
            home={<Welcome />}
            renderPending={(item) => <Pending item={item} />}
            renderBlock={(segment) => <GenerativeBlock block={segment.block} status={segment.status} onAdd={quickAdd} />}
          />
        </div>
        <div className="border-t border-(--line) p-3">
          <Composer send={(text) => void chat.send(text)} ready={chat.ready} busy={chat.busy} label={`Message the ${assistantName}`} placeholder="Ask about a product, a price, a policy…" variant="field" />
        </div>
      </aside>
    </>
  );
}
