"use client";

import { useState } from "react";
import Link from "next/link";

import { api } from "@/lib/api";
import { useApi } from "@/lib/use-api";
import { dateTime, duration, languageName, number, phone, rupees } from "@/lib/format";
import { ResolutionBadge, SentimentBadge, StatusBadge } from "@/components/call-badges";
import { Card, EmptyState, ErrorNotice, Spinner } from "@/components/ui";

const PAGE_SIZE = 25;

const STATUSES = [
  "",
  "completed",
  "in_progress",
  "no_answer",
  "busy",
  "failed",
  "blocked_dnd",
  "blocked_calling_hours",
] as const;

export default function CallsPage() {
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState("");
  const [direction, setDirection] = useState("");

  const calls = useApi(
    () =>
      api.calls.list({
        limit: PAGE_SIZE,
        offset,
        status: status || undefined,
        direction: direction || undefined,
      }),
    [offset, status, direction],
  );

  // Any filter change invalidates the current page number.
  function applyFilter(next: () => void) {
    setOffset(0);
    next();
  }

  const total = calls.data?.total ?? 0;
  const shown = calls.data?.items.length ?? 0;

  return (
    <div className="mx-auto max-w-6xl">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-ink-900">Calls</h1>
          <p className="text-sm text-ink-500">
            Every call your agents handled, newest first.
          </p>
        </div>
        <div className="flex gap-2">
          <select
            value={status}
            onChange={(e) => applyFilter(() => setStatus(e.target.value))}
            aria-label="Filter by status"
            className="rounded-lg border border-ink-200 bg-white px-3 py-1.5 text-sm text-ink-700"
          >
            {STATUSES.map((value) => (
              <option key={value} value={value}>
                {value === "" ? "All statuses" : value.replace(/_/g, " ")}
              </option>
            ))}
          </select>
          <select
            value={direction}
            onChange={(e) => applyFilter(() => setDirection(e.target.value))}
            aria-label="Filter by direction"
            className="rounded-lg border border-ink-200 bg-white px-3 py-1.5 text-sm text-ink-700"
          >
            <option value="">Both directions</option>
            <option value="inbound">Inbound</option>
            <option value="outbound">Outbound</option>
          </select>
        </div>
      </div>

      <Card className="mt-5 overflow-hidden">
        {calls.error ? (
          <div className="p-5">
            <ErrorNotice message={calls.error} onRetry={calls.reload} />
          </div>
        ) : calls.loading && !calls.data ? (
          <div className="p-5">
            <Spinner label="Loading calls" />
          </div>
        ) : shown === 0 ? (
          <EmptyState
            title="No calls match these filters"
            description="Once your agents start handling calls they will appear here with transcripts, sentiment and cost."
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[880px] text-sm">
              <thead>
                <tr className="border-b border-ink-100 text-left text-xs uppercase tracking-wide text-ink-500">
                  <th className="px-5 py-2.5 font-medium">When</th>
                  <th className="px-3 py-2.5 font-medium">Caller</th>
                  <th className="px-3 py-2.5 font-medium">Dir</th>
                  <th className="px-3 py-2.5 font-medium">Status</th>
                  <th className="px-3 py-2.5 font-medium">Outcome</th>
                  <th className="px-3 py-2.5 font-medium">Sentiment</th>
                  <th className="px-3 py-2.5 font-medium">Lang</th>
                  <th className="px-3 py-2.5 text-right font-medium">Length</th>
                  <th className="px-5 py-2.5 text-right font-medium">Cost</th>
                </tr>
              </thead>
              <tbody>
                {calls.data?.items.map((call) => (
                  <tr
                    key={call.id}
                    className="border-b border-ink-50 transition last:border-0 hover:bg-ink-50"
                  >
                    <td className="px-5 py-3 whitespace-nowrap">
                      <Link
                        href={`/calls/${call.id}`}
                        className="font-medium text-brand-700 hover:underline"
                      >
                        {dateTime(call.created_at)}
                      </Link>
                    </td>
                    <td className="px-3 py-3 whitespace-nowrap text-ink-700">
                      {phone(call.caller_number)}
                    </td>
                    <td className="px-3 py-3 text-ink-500 capitalize">
                      {call.direction}
                    </td>
                    <td className="px-3 py-3">
                      <StatusBadge status={call.status} />
                    </td>
                    <td className="px-3 py-3">
                      <ResolutionBadge resolution={call.resolution} />
                    </td>
                    <td className="px-3 py-3">
                      <SentimentBadge sentiment={call.sentiment} />
                    </td>
                    <td className="px-3 py-3 text-ink-500">
                      {languageName(call.language)}
                    </td>
                    <td className="tnum px-3 py-3 text-right text-ink-600">
                      {duration(call.duration_sec)}
                    </td>
                    <td className="tnum px-5 py-3 text-right text-ink-600">
                      {rupees(call.cost_paise)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {shown > 0 ? (
          <div className="flex items-center justify-between border-t border-ink-100 px-5 py-3 text-sm">
            <span className="text-ink-500">
              {number(offset + 1)}–{number(offset + shown)} of {number(total)}
            </span>
            <div className="flex gap-2">
              <button
                type="button"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                className="rounded-lg border border-ink-200 px-3 py-1.5 font-medium text-ink-700 disabled:opacity-40"
              >
                Previous
              </button>
              <button
                type="button"
                disabled={offset + shown >= total}
                onClick={() => setOffset(offset + PAGE_SIZE)}
                className="rounded-lg border border-ink-200 px-3 py-1.5 font-medium text-ink-700 disabled:opacity-40"
              >
                Next
              </button>
            </div>
          </div>
        ) : null}
      </Card>
    </div>
  );
}
