// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Adapted from Anthropic's retail merchant portal example (Apache-2.0).

import { AgentApi } from "web-shared";
import type { AlertsResponse, ListingDetailResponse, ListingsResponse, OverviewResponse } from "./types";

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8007";

export const api = new AgentApi(API_URL, "/api/merchant");

export const UNREACHABLE =
  "Couldn't reach the merchant API on port 8007. Start it with " +
  "`uvicorn merchant.api.main:app --port 8007` and try again.";

export function fetchOverview(): Promise<OverviewResponse | null> {
  return api.get<OverviewResponse>("/overview");
}

export function fetchListings(query?: string): Promise<ListingsResponse | null> {
  return api.get<ListingsResponse>("/listings", query ? { query } : undefined);
}

/** Listing ids are WooCommerce product ids (numeric strings); encoded all the same. */
export function fetchListingDetail(listingId: string): Promise<ListingDetailResponse | null> {
  return api.get<ListingDetailResponse>(`/listings/${encodeURIComponent(listingId)}`);
}

export function fetchAlerts(): Promise<AlertsResponse | null> {
  return api.get<AlertsResponse>("/alerts");
}
