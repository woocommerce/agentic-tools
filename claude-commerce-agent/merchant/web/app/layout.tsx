// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// This app is adapted from the retail merchant portal example in Anthropic's
// commerce-agents (Copyright 2026 Anthropic PBC, Apache-2.0), reworked for the
// WooCommerce merchant API served by merchant/api.

import type { Metadata } from "next";
import { Instrument_Sans } from "next/font/google";
import "./globals.css";

const instrumentSans = Instrument_Sans({
  subsets: ["latin"],
  variable: "--font-body",
  display: "swap",
});

export const metadata: Metadata = {
  title: "WooCommerce Merchant",
  description: "A WooCommerce store's back office, run through the merchant agent.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={instrumentSans.variable}>
      <body>{children}</body>
    </html>
  );
}
