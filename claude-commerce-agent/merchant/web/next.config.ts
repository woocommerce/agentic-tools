// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// This app is adapted from the retail merchant portal example in Anthropic's
// commerce-agents (Copyright 2026 Anthropic PBC, Apache-2.0), reworked for the
// WooCommerce merchant API served by merchant/api.

import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  transpilePackages: ["web-shared"],
};

export default nextConfig;
