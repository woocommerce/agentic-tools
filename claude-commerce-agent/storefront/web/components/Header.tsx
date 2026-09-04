// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Written for the WooCommerce storefront; the retail example's app bar lives in its shared shell.

"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Avatar, formatMoney, Icon } from "web-shared";
import { brandInitials } from "@/lib/brand";
import { useStore } from "@/lib/store";

const NAV = [
  { href: "/", label: "Shop" },
  { href: "/orders", label: "Orders" },
] as const;

function StoreMark() {
  const { brand } = useStore();
  if (brand?.logo_url) {
    // eslint-disable-next-line @next/next/no-img-element
    return <img src={brand.logo_url} alt="" className="h-[30px] w-auto max-w-[120px] object-contain" />;
  }
  return (
    <span aria-hidden className="grid h-[30px] w-[30px] place-items-center rounded-lg bg-(--brand) text-[13px] font-bold text-(--brand-contrast)">
      {brand ? brandInitials(brand.name) : "W"}
    </span>
  );
}

/** The store's bar: its mark, the pages, and the buttons for the shopper's sheet, the cart, and the assistant. */
export default function Header({ onOpenCart, onOpenAssistant, onOpenAccount }: { onOpenCart: () => void; onOpenAssistant: () => void; onOpenAccount: () => void }) {
  const { brand, cart, chat } = useStore();
  const pathname = usePathname();
  const count = cart?.item_count ?? 0;
  return (
    <header className="flex h-[58px] shrink-0 items-center gap-2 border-b border-(--line) bg-(--chrome) px-3 sm:gap-5 sm:px-5">
      <Link href="/" className="flex shrink-0 items-center gap-2.5 pr-1">
        <StoreMark />
        <span className="max-w-[40vw] truncate text-[17px] font-bold tracking-[-0.02em] text-(--ink) sm:max-w-none">{brand?.name ?? "Storefront"}</span>
      </Link>
      <nav className="flex min-w-0 items-center gap-1" aria-label="Pages">
        {NAV.map((item) => {
          const active = item.href === "/" ? pathname === "/" || pathname.startsWith("/products") : pathname.startsWith(item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={active ? "page" : undefined}
              className={`rounded-[9px] px-2.5 py-1.5 text-[14px] transition-colors ${active ? "bg-(--well) font-semibold text-(--ink)" : "font-medium text-(--ink-2) hover:bg-(--well)/60"}`}
            >
              {item.label}
            </Link>
          );
        })}
      </nav>
      <div className="ml-auto flex items-center gap-2">
        <button
          type="button"
          onClick={onOpenCart}
          aria-label={`Open cart, ${count} item${count === 1 ? "" : "s"}`}
          className="flex h-[34px] items-center gap-2 rounded-full bg-(--ink) pl-3 pr-1.5 text-[13px] font-semibold text-(--surface) transition hover:brightness-110"
        >
          <Icon name="bag" size={16} />
          <span className="hidden sm:inline">Cart</span>
          {count ? <span className="hidden tabular-nums md:inline">· {formatMoney(cart?.subtotal ?? 0, cart?.currency)}</span> : null}
          <span key={count} data-cart-target className="ac-pop grid h-[22px] min-w-[22px] place-items-center rounded-full bg-(--surface) px-1 text-[11.5px] font-bold tabular-nums text-(--ink)">
            {count}
          </span>
        </button>
        <button
          type="button"
          onClick={onOpenAssistant}
          aria-label="Open the assistant"
          className="relative grid h-[34px] w-[34px] place-items-center rounded-full bg-(--accent) text-(--on-accent) transition hover:brightness-95 lg:hidden"
        >
          <Icon name="spark" size={16} />
          {chat.streaming ? <span className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 animate-pulse rounded-full border-2 border-(--chrome) bg-(--accent)" aria-hidden /> : null}
        </button>
        <button type="button" onClick={onOpenAccount} aria-label="Guest: what the assistant remembers" className="flex items-center gap-2.5 rounded-full py-0.5 pl-0.5 pr-1 text-left transition-colors hover:bg-(--well)/60 md:pr-3">
          <Avatar name="Guest" />
          <span className="hidden min-w-0 md:block">
            <span className="block truncate text-[13px] font-semibold leading-tight">Guest</span>
            <span className="block truncate text-[11.5px] leading-tight text-(--ink-soft)">Browsing</span>
          </span>
        </button>
      </div>
    </header>
  );
}
