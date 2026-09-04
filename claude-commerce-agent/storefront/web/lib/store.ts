// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Written for the WooCommerce storefront: the pages under the shell read the store's state
// from here instead of taking it as props.

"use client";

import { createContext, useContext } from "react";
import type { AgentTurn } from "web-shared";
import type { AddResult } from "./api";
import type { Brand, CartPayload, Product } from "./types";

export interface StoreState {
  brand: Brand | null;
  sessionId: string | null;
  chat: AgentTurn;
  cart: CartPayload | null;
  assistantName: string;
  /** The add button's write, with the host's reason when it refuses. */
  addToCart: (productId: string, quantity?: number) => Promise<AddResult>;
  /** A card's one-tap add: true once the cart holds it; a refusal is shown as a notice. */
  quickAdd: (product: Product) => Promise<boolean>;
  /** Sends a message to the assistant and brings the conversation into view. */
  ask: (message: string) => void;
  openCart: () => void;
  /** A short line shown at the foot of the page for a few seconds. */
  notify: (text: string) => void;
}

export const StoreContext = createContext<StoreState | null>(null);

export function useStore(): StoreState {
  const value = useContext(StoreContext);
  if (!value) throw new Error("useStore needs a StoreShell above it");
  return value;
}
