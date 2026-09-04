// Copyright 2026 Anthropic PBC
// SPDX-License-Identifier: Apache-2.0

"use client";

import type { ReactNode } from "react";
import { AssistantText, ErrorBubble, UserBubble } from "./MessageBubble";
import type { AssistantChatItem, ChatItem, UISegment } from "./protocol";
import { Suggestions } from "./Suggestions";

export interface TranscriptProps {
  items: ChatItem[];
  busy: boolean;
  send: (text: string) => void;
  renderBlock: (segment: UISegment, item: AssistantChatItem) => ReactNode;
  /** Defaults to `ActivityLine`. */
  renderPending?: (item: AssistantChatItem) => ReactNode;
  suggestionFilter?: (text: string) => boolean;
  /** Components that may extend past the text measure when the page has room. */
  wide?: ReadonlySet<string>;
  gap?: string;
}

/** What shows under a reply while it is being made: the current step, or a shimmer before the first word. */
export function ActivityLine({ item }: { item: AssistantChatItem }) {
  if (item.activity) {
    return (
      <div role="status" className="flex items-center gap-2 text-[13px] text-(--ink-soft)">
        <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-(--accent)" />
        <span className="min-w-0 truncate">{item.activity}</span>
      </div>
    );
  }
  if (item.segments.length) return null;
  return (
    <div role="status" aria-label="Working" className="flex flex-col gap-2">
      <div className="ac-skeleton h-4 w-3/5 rounded" />
      <div className="ac-skeleton h-4 w-2/5 rounded" />
    </div>
  );
}

export function Transcript({
  items,
  busy,
  send,
  renderBlock,
  renderPending = (item) => <ActivityLine item={item} />,
  suggestionFilter,
  wide,
  gap = "gap-3",
}: TranscriptProps) {
  return items.map((item, index) =>
    item.kind === "user" ? (
      <UserBubble key={index} text={item.text} />
    ) : (
      <div key={index} data-turn={item.turn} className={`flex flex-col ${gap}`}>
        {item.segments.map((segment, i) => {
          if (segment.type === "text") {
            const last = item.pending && i === item.segments.length - 1;
            return <AssistantText key={i} text={segment.text} streaming={last} />;
          }
          if (segment.type === "error") return <ErrorBubble key={i} text={segment.text} />;
          return (
            <div
              key={segment.slotKey}
              data-component={segment.block.component}
              className={`transition-opacity duration-300 ${segment.status === "retrying" ? "opacity-60" : ""} ${
                wide?.has(segment.block.component) ? "min-[1500px]:-mx-14" : ""
              }`}
            >
              {renderBlock(segment, item)}
            </div>
          );
        })}
        {item.pending ? renderPending(item) : null}
        {!item.pending && index === items.length - 1 ? (
          <Suggestions
            suggestions={suggestionFilter ? item.suggestions.filter(suggestionFilter) : item.suggestions}
            onPick={send}
            disabled={busy || item.suggestionsStale}
          />
        ) : null}
      </div>
    ),
  );
}

export function LatestPill({ onClick }: { onClick: () => void }) {
  return (
    <div className="pointer-events-none absolute inset-x-0 bottom-3 flex justify-center">
      <button
        type="button"
        onClick={onClick}
        className="pointer-events-auto rounded-full border border-(--line) bg-(--card) px-3.5 py-1.5 text-[13px] font-semibold text-(--ink) shadow-md transition hover:border-(--accent)"
      >
        ↓ Latest
      </button>
    </div>
  );
}
