// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// This app is adapted from the retail merchant portal example in Anthropic's
// commerce-agents (Copyright 2026 Anthropic PBC, Apache-2.0), reworked for the
// WooCommerce merchant API served by merchant/api.

"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AssistantRail,
  Inspector,
  type PortalNavItem,
  PortalShell,
  type Prefill,
  useMerchantChat,
  useResource,
  useSession,
} from "web-shared";
import AssistantPanel from "@/components/AssistantPanel";
import CatalogView from "@/components/views/CatalogView";
import HomeView from "@/components/views/HomeView";
import InventoryView from "@/components/views/InventoryView";
import OrdersView from "@/components/views/OrdersView";
import { api, fetchOverview, UNREACHABLE } from "@/lib/api";
import { storeDetail } from "@/lib/format";
import type { StagedChange } from "@/lib/types";

type PortalView = "home" | "catalog" | "orders" | "inventory";

function StoreMark() {
  return (
    <span
      aria-hidden
      className="grid h-[34px] w-[34px] shrink-0 place-items-center rounded-[10px] bg-(--brand) text-[16px] font-bold text-white shadow-[inset_0_-3px_0_rgba(0,0,0,0.18)]"
    >
      W
    </span>
  );
}

export default function PortalPage() {
  const session = useSession(api);
  const [view, setView] = useState<PortalView>("home");
  const [assistantOpen, setAssistantOpen] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);
  const [prefill, setPrefill] = useState<Prefill | null>(null);
  // Bumped whenever a staged change moves, so every widget re-reads the store the agent wrote.
  const [refreshKey, setRefreshKey] = useState(0);
  const refreshPortal = useCallback(() => setRefreshKey((value) => value + 1), []);

  const chat = useMerchantChat<StagedChange>(api, {
    ...session,
    unreachable: UNREACHABLE,
    onPortalRefresh: refreshPortal,
  });

  // The overview feeds the home page, the sidebar counts, and the store line under the mark.
  const { data: overview, failed: overviewFailed } = useResource(session.sessionId ? fetchOverview : null, [session.sessionId, refreshKey]);

  // The rail is part of the default layout on wide screens; narrow screens open it on demand.
  useEffect(() => {
    setAssistantOpen(window.innerWidth >= 1024);
  }, []);

  const askAssistant = useCallback((text: string) => {
    setAssistantOpen(true);
    setPrefill({ text, nonce: Date.now() });
  }, []);

  const nav = useMemo<PortalNavItem<PortalView>[]>(() => {
    const alerts = overview?.snapshot.alerts;
    return [
      { id: "home", label: "Home", icon: "home" },
      { id: "catalog", label: "Catalog", icon: "tag" },
      { id: "orders", label: "Orders", icon: "inbox", attention: alerts?.order_issues || null },
      {
        id: "inventory",
        label: "Inventory",
        icon: "box",
        count: alerts ? (alerts.low_stock ?? 0) + (alerts.slow_movers ?? 0) : null,
      },
    ];
  }, [overview]);

  const currency = overview?.snapshot.currency;

  return (
    <>
      <PortalShell
        brand={{ mark: <StoreMark />, name: "WooCommerce", detail: storeDetail(overview?.store_kind, overview?.store_url) }}
        nav={nav}
        view={view}
        onViewChange={setView}
        operator={{ name: session.operator ?? "Operator", role: "Store manager" }}
        assistantOpen={assistantOpen}
        assistantBusy={chat.busy}
        onToggleAssistant={() => setAssistantOpen((open) => !open)}
        rail={
          <AssistantRail open={assistantOpen} storageKey="woocommerce-merchant-panel-width" onClose={() => setAssistantOpen(false)}>
            {(rail) => (
              <AssistantPanel
                chat={chat}
                prefill={prefill}
                onPrefill={askAssistant}
                newMemoryCount={chat.newMemoryKeys.size}
                onOpenActivity={() => setActivityOpen(true)}
                {...rail}
              />
            )}
          </AssistantRail>
        }
      >
        {session.sessionId ? (
          <>
            {view === "home" ? (
              <HomeView data={overview} failed={overviewFailed} operator={session.operator} onAskAssistant={askAssistant} onNavigate={setView} />
            ) : null}
            {view === "catalog" ? <CatalogView refreshKey={refreshKey} onAskAssistant={askAssistant} /> : null}
            {view === "orders" ? (
              <OrdersView
                refreshKey={refreshKey}
                recentOrders={overview?.recent_orders ?? (overviewFailed ? [] : null)}
                currency={currency}
                onAskAssistant={askAssistant}
              />
            ) : null}
            {view === "inventory" ? <InventoryView refreshKey={refreshKey} onAskAssistant={askAssistant} /> : null}
          </>
        ) : null}
      </PortalShell>
      {activityOpen ? (
        <Inspector
          turnCount={chat.turnCount}
          streaming={chat.streaming}
          trace={chat.trace}
          memory={chat.memory}
          newMemoryKeys={chat.newMemoryKeys}
          memoryTitle="Business memory"
          onClose={() => setActivityOpen(false)}
        />
      ) : null}
    </>
  );
}
