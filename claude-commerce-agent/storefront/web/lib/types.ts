// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Adapted from Anthropic's retail storefront example (Apache-2.0).

/**
 * What the storefront API sends this page. Products follow shopping_agent/types.py; the
 * cart payload is the shared host's plus the two keys storefront/api/main.py stamps on it.
 */

export interface Product {
  /** A WooCommerce post id, as a string. */
  product_id: string;
  title: string;
  brand?: string | null;
  price: number;
  currency?: string;
  rating?: number | null;
  review_count?: number | null;
  image_url?: string | null;
  category?: string | null;
  labels?: string[];
  /** WooCommerce fills `sku` and, on a discounted product, `regular_price`. */
  attributes?: Record<string, string>;
  in_stock?: boolean;
  short_description?: string | null;
  /** A variable product's options; the cart only takes one of its variations. */
  options?: Record<string, string[]>;
  /** The option value a variation carries for each option. */
  option_values?: Record<string, string>;
  /** The variable product a variation belongs to. */
  variant_of?: string | null;
}

export interface ProductDetails extends Product {
  /** The variations of a variable product, each under its own id. */
  variants?: Product[];
  long_description?: string | null;
  specs?: Record<string, string>;
  review_highlights?: string[];
}

export interface CartItem {
  product_id: string;
  title: string;
  price: number;
  quantity: number;
  image_url?: string | null;
  option_values?: Record<string, string>;
  variant_of?: string | null;
  line_total: number;
}

export interface CartPayload {
  items: CartItem[];
  item_count: number;
  subtotal: number;
  currency: string;
  /** The store's own checkout page for this cart; null until the store issued one. */
  checkout_url: string | null;
  /** The Store API cart token; the page keeps it so a later session can rejoin the cart. */
  cart_token: string | null;
}

/** GET /api/brand: the site's name and look, read by the host from WordPress. */
export interface Brand {
  name: string;
  slogan?: string | null;
  tagline?: string | null;
  logo_url?: string | null;
  cover_image_url?: string | null;
  /** Present only when the theme's colours pass the host's contrast check. */
  colors?: { background: string; foreground: string };
}

// --- Presentation payloads the assistant streams ---

export interface ProductsPayload {
  title?: string;
  layout?: "carousel" | "grid" | "list";
  items: { product: Product; reason?: string | null }[];
}

export interface ComparisonPayload {
  title?: string;
  entries: {
    product_id: string;
    product: Product;
    pros?: string[];
    cons?: string[];
    best_for?: string | null;
  }[];
  dimensions?: string[];
  recommended_product_id?: string | null;
  /** Added by the server: the gap between the cheapest and the dearest entry. */
  price_delta?: {
    amount: number;
    low_product_id: string;
    low_price: number;
    high_product_id: string;
    high_price: number;
  };
}

export interface PlanPayload {
  title: string;
  intro?: string;
  steps: { label: string; detail?: string | null; products: Product[] }[];
}

export interface GuidePayload {
  title: string;
  sections: { heading: string; body: string }[];
  related_products?: Product[];
  sources?: string[];
}

export interface OrderStatusPayload {
  order_id: string;
  summary: string;
  next_step?: string;
  order?: {
    order_id: string;
    status: string;
    placed_at: string;
    items: { product_id: string; title: string; quantity: number; price: number }[];
    total: number;
    currency?: string;
    estimated_delivery?: string;
    tracking_url?: string;
  };
}

export interface CheckoutHandoff {
  url: string;
  label?: string;
  seller?: string;
}

export interface CheckoutPayload {
  /** The backend's checkout link(s); for WooCommerce, the store's checkout page. */
  handoffs?: CheckoutHandoff[];
  note?: string;
  fulfillment_method?: "delivery" | "pickup" | "shipping";
  cart: Omit<CartPayload, "checkout_url" | "cart_token">;
}
