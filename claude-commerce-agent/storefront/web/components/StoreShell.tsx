// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Adapted from Anthropic's retail storefront example (Apache-2.0): its page held the session,
// the conversation, and the cart; here the shell holds them for every route under it.

"use client";

import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { type AgentEvent, Inspector, useAgentTurn } from "web-shared";
import { FrameContext } from "web-shared/storefront/frame";
import { AccountSheet } from "web-shared/storefront/home";
import { addToCart as postAdd, api, fetchBrand, fetchCart, UNREACHABLE } from "@/lib/api";
import { applyBrandColors, assistantNameFor } from "@/lib/brand";
import { rememberCartToken, useStoreSession } from "@/lib/session";
import { StoreContext, type StoreState } from "@/lib/store";
import type { Brand, CartPayload, Product } from "@/lib/types";
import AssistantRail from "./AssistantRail";
import CartDrawer from "./CartDrawer";
import Header from "./Header";

const NOTICE_MS = 6000;

export default function StoreShell({ children }: { children: ReactNode }) {
  const { sessionId, startupCart, failed } = useStoreSession(api);
  const [brand, setBrand] = useState<Brand | null>(null);
  const [cart, setCart] = useState<CartPayload | null>(null);
  const [cartOpen, setCartOpen] = useState(false);
  const [assistantOpen, setAssistantOpen] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);
  const [accountOpen, setAccountOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  /** Every cart the host sends passes through here, so the token is always the latest. */
  const applyCart = useCallback((next: CartPayload | null) => {
    if (!next) return;
    setCart(next);
    rememberCartToken(next.cart_token);
  }, []);
  useEffect(() => applyCart(startupCart), [startupCart, applyCart]);

  const refreshCart = useCallback(async () => applyCart(await fetchCart()), [applyCart]);

  const onEvent = useCallback(
    (event: AgentEvent) => {
      if (event.type !== "cart_update") return;
      const next = event.data.cart as Omit<CartPayload, "checkout_url" | "cart_token"> | undefined;
      if (!next) return;
      // The event carries the lines; the link and token come with the re-read.
      setCart((previous) => ({ ...next, checkout_url: previous?.checkout_url ?? null, cart_token: previous?.cart_token ?? null }));
      void refreshCart();
    },
    [refreshCart],
  );
  const chat = useAgentTurn(api, { sessionId, unreachable: UNREACHABLE, onEvent });

  useEffect(() => {
    void fetchBrand().then((loaded) => {
      if (!loaded) return;
      setBrand(loaded);
      applyBrandColors(loaded);
      document.title = loaded.name;
    });
  }, []);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), NOTICE_MS);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const { send } = chat;
  const ask = useCallback(
    (message: string) => {
      setCartOpen(false);
      setAssistantOpen(true);
      void send(message);
    },
    [send],
  );
  const closeCart = useCallback(() => setCartOpen(false), []);
  const assistantName = assistantNameFor(brand);

  const addToCart = useCallback<StoreState["addToCart"]>(
    async (productId, quantity = 1) => {
      const result = await postAdd(productId, quantity);
      if (result.ok) applyCart(result.cart);
      return result;
    },
    [applyCart],
  );
  const quickAdd = useCallback(
    async (product: Product) => {
      const result = await addToCart(product.product_id);
      if (!result.ok) setNotice(result.reason);
      return result.ok;
    },
    [addToCart],
  );

  const store = useMemo<StoreState>(
    () => ({
      brand,
      sessionId,
      chat,
      cart,
      assistantName,
      addToCart,
      quickAdd,
      ask,
      openCart: () => setCartOpen(true),
      notify: setNotice,
    }),
    [brand, sessionId, chat, cart, assistantName, addToCart, quickAdd, ask],
  );
  // The shared bag and home pieces read the conversation and `ask` from this frame.
  const frame = useMemo(() => ({ chat, assistantName, ask, closePanel: closeCart }), [chat, assistantName, ask, closeCart]);

  return (
    <StoreContext.Provider value={store}>
      <FrameContext.Provider value={frame}>
        <div className="flex h-dvh flex-col text-(--ink)">
          <Header onOpenCart={() => setCartOpen(true)} onOpenAssistant={() => setAssistantOpen(true)} onOpenAccount={() => setAccountOpen(true)} />
          {failed ? (
            <div role="alert" className="border-b border-(--danger)/30 bg-(--danger-soft) px-4 py-2 text-center text-[13px] text-(--danger)">
              {UNREACHABLE}
            </div>
          ) : null}
          <div className="flex min-h-0 flex-1">
            <main className="panel-scroll relative min-w-0 flex-1 overflow-y-auto">
              {children}
              {notice ? (
                <div role="status" className="pointer-events-none sticky bottom-4 flex justify-center px-4">
                  <div className="pointer-events-auto max-w-[520px] rounded-xl border border-(--line) bg-(--card) px-4 py-2.5 text-[13px] leading-snug text-(--ink) shadow-(--shadow-lg)">{notice}</div>
                </div>
              ) : null}
            </main>
            <AssistantRail open={assistantOpen} onClose={() => setAssistantOpen(false)} onOpenActivity={() => setActivityOpen(true)} />
          </div>
          <CartDrawer open={cartOpen} onClose={closeCart} />
          {accountOpen ? <AccountSheet name="Guest" detail="Browsing without an account" api={api} onClose={() => setAccountOpen(false)} /> : null}
          {activityOpen ? (
            <Inspector
              turnCount={chat.turnCount}
              streaming={chat.streaming}
              trace={chat.trace}
              memory={chat.memory}
              newMemoryKeys={chat.newMemoryKeys}
              memoryTitle={`What the ${assistantName} knows`}
              onClose={() => setActivityOpen(false)}
            />
          ) : null}
        </div>
      </FrameContext.Provider>
    </StoreContext.Provider>
  );
}
