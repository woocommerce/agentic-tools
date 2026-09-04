// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Adapted from Anthropic's retail merchant portal example (Apache-2.0).

"use client";

import { AttentionList, AttentionRow, formatDayMonth, Notice, PageHeader, Panel, plural, QuotedAsData, RecordList, Skeleton, useResource } from "web-shared";
import { fetchAlerts } from "@/lib/api";
import { orderRows } from "@/lib/format";
import { issueStyle } from "@/lib/kinds";
import type { OrderIssue, RecentOrder } from "@/lib/types";

function IssueRow({ issue, onAskAssistant }: { issue: OrderIssue; onAskAssistant: (text: string) => void }) {
  const style = issueStyle(issue.kind);
  return (
    <AttentionRow
      icon={style.icon}
      tone={style.tone}
      title={issue.summary}
      meta={[style.label, `Order #${issue.order_id}`, issue.listing_id ? `product #${issue.listing_id}` : "", issue.opened_at ? `opened ${formatDayMonth(issue.opened_at)}` : ""]
        .filter(Boolean)
        .join(" · ")}
      note={
        issue.buyer_message_excerpt ? (
          <div className="mt-1 rounded-[10px] bg-(--ground) px-3 py-2">
            <blockquote className="text-[13px] leading-snug text-(--ink-2)">&ldquo;{issue.buyer_message_excerpt}&rdquo;</blockquote>
            {/* An order note is buyer-written text, so the note sits beside the quote. */}
            <QuotedAsData subject="Buyer message" className="mt-1.5" />
          </div>
        ) : null
      }
      action={{
        label: issue.kind === "buyer_message" ? "Draft reply" : "Ask",
        onClick: () => onAskAssistant(`What are my options for order ${issue.order_id}? ${issue.summary}.`),
      }}
    />
  );
}

export default function OrdersView({
  refreshKey,
  recentOrders,
  currency,
  onAskAssistant,
}: {
  refreshKey: number;
  recentOrders: RecentOrder[] | null;
  currency?: string;
  onAskAssistant: (text: string) => void;
}) {
  const { data, failed } = useResource(fetchAlerts, [refreshKey]);
  const issues = data?.order_issues ?? [];

  return (
    <div className="ac-reveal flex flex-col gap-4">
      <PageHeader title="Orders" subtitle={data ? (issues.length ? plural(issues.length, "open issue") : "No open issues") : undefined} />
      {failed && !data ? (
        <Notice>The merchant API isn&apos;t reachable, so order issues can&apos;t load.</Notice>
      ) : !data ? (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
          <Skeleton className="h-96" />
          <Skeleton className="h-64" />
        </div>
      ) : (
        <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
          <Panel title="Open issues" subtitle={issues.length ? String(issues.length) : undefined}>
            {issues.length === 0 ? (
              <p className="px-[18px] pb-4 text-[13.5px] text-(--ink-soft)">No open order issues.</p>
            ) : (
              <AttentionList>
                {issues.map((issue) => (
                  <IssueRow key={issue.issue_id} issue={issue} onAskAssistant={onAskAssistant} />
                ))}
              </AttentionList>
            )}
          </Panel>
          <Panel title="Recent orders">
            {!recentOrders ? (
              <Skeleton className="mx-[18px] mb-4 h-40" />
            ) : recentOrders.length === 0 ? (
              <p className="px-[18px] pb-4 text-[13px] text-(--ink-soft)">No recent orders to show.</p>
            ) : (
              <RecordList rows={orderRows(recentOrders, currency)} />
            )}
          </Panel>
        </div>
      )}
    </div>
  );
}
