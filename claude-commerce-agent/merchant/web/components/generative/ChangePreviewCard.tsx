// Copyright 2026 Automattic Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Adapted from Anthropic's retail merchant portal example (Apache-2.0).

"use client";

import {
  ApproveBar,
  type ChangeAction,
  ChangeStatusPill,
  coverLabel,
  describeProposer,
  DiffRows,
  formatDate,
  formatMoney,
  GenCard,
  GenCardHeader,
  GuardrailNotes,
  isLongTextDiff,
  LongTextDiff,
  titleCase,
  useChangeActions,
  useResource,
} from "web-shared";
import { fetchListingDetail } from "@/lib/api";
import type { ChangeItem, ChangePreviewPayload, StagedChange } from "@/lib/types";

/** Days of cover = new stock / (sales_last_30d / 30). */
function RestockMath({ item }: { item: ChangeItem }) {
  const { data: detail } = useResource(() => fetchListingDetail(item.target), [item.target]);
  const sales30 = detail?.listing.sales_last_30d ?? null;
  if (typeof item.before !== "number" || typeof item.after !== "number") return null;
  const added = item.after - item.before;
  if (added <= 0 || sales30 == null || sales30 <= 0) return null;
  const perDay = sales30 / 30;
  const coverDays = item.after / perDay;
  return (
    <p className="mx-3.5 mt-2 text-[12.5px] tabular-nums text-(--ink-soft)">
      +{added} units · sells <b className="font-semibold text-(--ink)">{perDay.toFixed(1)} a day</b> · {item.after} on hand ≈{" "}
      <b className="font-semibold text-(--ink)">{coverLabel(coverDays)}</b>
    </p>
  );
}

/** Restocks add the days-of-cover math under the diff. */
export default function ChangePreviewCard({
  payload,
  onAct,
}: {
  payload: ChangePreviewPayload;
  onAct?: (changeId: string, action: ChangeAction) => Promise<StagedChange | null>;
}) {
  const { change, busy, error, act, canAct } = useChangeActions(payload.change, onAct);
  const shortItems = change.items.filter((item) => !isLongTextDiff(item));
  const longItems = change.items.filter(isLongTextDiff);

  return (
    <GenCard>
      <GenCardHeader
        title={payload.headline ?? "Proposed change"}
        meta={
          <>
            <ChangeStatusPill status={change.status} />
            <span>{titleCase(change.kind)}</span>
            <span aria-hidden>·</span>
            <span>{describeProposer(change)}</span>
            <span aria-hidden>·</span>
            <span>{formatDate(change.created_at)}</span>
          </>
        }
      />
      <p className="px-3.5 pt-2 text-[14px] leading-snug text-(--ink)">{change.summary}</p>
      {payload.note ? <p className="px-3.5 pt-1 text-[12.5px] leading-snug text-(--ink-soft)">{payload.note}</p> : null}

      <DiffRows items={shortItems} />
      {longItems.map((item, index) => (
        <LongTextDiff key={`${item.target}-${item.field}-${index}`} item={item} />
      ))}
      {change.kind === "inventory_action"
        ? change.items.filter((item) => item.field === "stock").map((item) => <RestockMath key={`${item.target}-math`} item={item} />)
        : null}

      {change.margin_impact != null ? (
        <p className="mx-3.5 mt-2 text-[12.5px] tabular-nums text-(--ink-soft)">
          Margin impact{" "}
          <b className={`font-semibold ${change.margin_impact < 0 ? "text-(--danger)" : "text-(--ok)"}`}>
            {change.margin_impact > 0 ? "+" : ""}
            {formatMoney(change.margin_impact, change.currency ?? undefined)}
          </b>
        </p>
      ) : null}

      <GuardrailNotes notes={change.guardrail_notes} />
      <ApproveBar change={change} busy={busy} error={error} canAct={canAct} onAct={(action) => void act(action)} />
    </GenCard>
  );
}
