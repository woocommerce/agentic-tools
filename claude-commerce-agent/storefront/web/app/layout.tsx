// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// This app is adapted from the retail storefront example in Anthropic's
// commerce-agents (Copyright 2026 Anthropic PBC, Apache-2.0), reworked for the
// WooCommerce storefront API served by storefront/api.

import type { Metadata } from "next";
import { Instrument_Sans } from "next/font/google";
import StoreShell from "@/components/StoreShell";
import "./globals.css";

const instrumentSans = Instrument_Sans({
  subsets: ["latin"],
  variable: "--font-body",
  display: "swap",
});

export const metadata: Metadata = {
  title: "WooCommerce Storefront",
  description: "A live WooCommerce store browsed through the shopping agent.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={instrumentSans.variable}>
      <body>
        {/* The shell keeps the session, the conversation, and the cart while the pages change. */}
        <StoreShell>{children}</StoreShell>
      </body>
    </html>
  );
}
