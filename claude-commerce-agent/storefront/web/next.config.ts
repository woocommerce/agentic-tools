// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// This app is adapted from the retail storefront example in Anthropic's
// commerce-agents (Copyright 2026 Anthropic PBC, Apache-2.0), reworked for the
// WooCommerce storefront API served by storefront/api.

import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  transpilePackages: ["web-shared"],
};

export default nextConfig;
