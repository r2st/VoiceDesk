"use client";

import { useState } from "react";

import { api } from "@/lib/api";
import { useApi } from "@/lib/use-api";
import { dateTime, number, percent, phone, titleCase } from "@/lib/format";
import {
  CrmBadge,
  LeadStatusBadge,
  ScoreBar,
  TierBadge,
} from "@/components/lead-badges";
import {
  Card,
  EmptyState,
  ErrorNotice,
  Spinner,
  StatCard,
} from "@/components/ui";
import type { LeadStatus, LeadTier } from "@/lib/types";

import { LeadDrawer } from "./lead-drawer";

const PAGE_SIZE = 25;

const TIERS: Array<LeadTier | ""> = ["", "hot", "warm", "cold", "unqualified"];

const STATUSES: Array<LeadStatus | ""> = [
  "",
  "qualified",
  "contacted",
  "converted",
  "lost",
  "disqualified",
];

export default function LeadsPage() {
  const [offset, setOffset] = useState(0);
  const [tier, setTier] = useState<LeadTier | "">("");
  const [status, setStatus] = useState<LeadStatus | "">("");
  const [openLeadId, setOpenLeadId] = useState<string | null>(null);

  const leads = useApi(
    () => api.leads.list({ limit: PAGE_SIZE, offset, tier, status }),
    [offset, tier, status],
  );
  const summary = useApi(() => api.leads.summary(), []);

  // Any filter change invalidates the current page number.
  function applyFilter(next: () => void) {
    setOffset(0);
    next();
  }

  /** A status change moves the lead between filters, so both views reload. */
  function refreshAll() {
    leads.reload();
    summary.reload();
  }

  const total = leads.data?.total ?? 0;
  const shown = leads.data?.items.length ?? 0;
  const stats = summary.data;

  return (
    <div className="mx-auto max-w-6xl">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-ink-900">Leads</h1>
          <p className="text-sm text-ink-500">
            Everyone your agents qualified, best score first.
          </p>
        </div>
        <div className="flex gap-2">
          <select
            value={tier}
            onChange={(e) =>
              applyFilter(() => setTier(e.target.value as LeadTier | ""))
            }
            aria-label="Filter by tier"
            className="rounded-lg border border-ink-200 bg-white px-3 py-1.5 text-sm text-ink-700"
          >
            {TIERS.map((value) => (
              <option key={value} value={value}>
                {value === "" ? "All tiers" : titleCase(value)}
              </option>
            ))}
          </select>
          <select
            value={status}
            onChange={(e) =>
              applyFilter(() => setStatus(e.target.value as LeadStatus | ""))
            }
            aria-label="Filter by status"
            className="rounded-lg border border-ink-200 bg-white px-3 py-1.5 text-sm text-ink-700"
          >
            {STATUSES.map((value) => (
              <option key={value} value={value}>
                {value === "" ? "All statuses" : titleCase(value)}
              </option>
            ))}
          </select>
        </div>
      </div>

      {stats ? (
        <div className="mt-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <StatCard label="In pipeline" value={number(stats.total)} />
          <StatCard
            label="Hot"
            value={number(stats.by_tier.hot ?? 0)}
            hint="worth calling today"
          />
          <StatCard
            label="Qualified"
            value={percent(stats.qualified_rate)}
            hint={`${number(stats.by_status.qualified ?? 0)} of ${number(stats.total)}`}
          />
          <StatCard label="Average score" value={stats.average_score.toFixed(1)} />
        </div>
      ) : null}

      <Card className="mt-5 overflow-hidden">
        {leads.error ? (
          <div className="p-5">
            <ErrorNotice message={leads.error} onRetry={leads.reload} />
          </div>
        ) : leads.loading && !leads.data ? (
          <div className="p-5">
            <Spinner label="Loading leads" />
          </div>
        ) : shown === 0 ? (
          <EmptyState
            title="No leads match these filters"
            description="Once a qualification agent takes a call, the prospect appears here scored on budget, authority, need and timeline."
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[880px] text-sm">
              <thead>
                <tr className="border-b border-ink-100 text-left text-xs uppercase tracking-wide text-ink-500">
                  <th className="px-5 py-2.5 font-medium">Contact</th>
                  <th className="px-3 py-2.5 font-medium">Company</th>
                  <th className="px-3 py-2.5 font-medium">Score</th>
                  <th className="px-3 py-2.5 font-medium">Tier</th>
                  <th className="px-3 py-2.5 font-medium">Status</th>
                  <th className="px-3 py-2.5 font-medium">CRM</th>
                  <th className="px-5 py-2.5 text-right font-medium">Captured</th>
                </tr>
              </thead>
              <tbody>
                {leads.data?.items.map((lead) => (
                  <tr
                    key={lead.id}
                    className="border-b border-ink-50 transition last:border-0 hover:bg-ink-50"
                  >
                    <td className="px-5 py-3">
                      <button
                        type="button"
                        onClick={() => setOpenLeadId(lead.id)}
                        className="text-left font-medium text-brand-700 hover:underline"
                      >
                        {lead.contact_name}
                      </button>
                      <p className="tnum text-xs text-ink-500">
                        {phone(lead.contact_phone)}
                      </p>
                    </td>
                    <td className="px-3 py-3 text-ink-600">
                      {lead.company ?? <span className="text-ink-400">—</span>}
                    </td>
                    <td className="px-3 py-3">
                      <ScoreBar score={lead.score} tier={lead.tier} />
                    </td>
                    <td className="px-3 py-3">
                      <TierBadge tier={lead.tier} />
                    </td>
                    <td className="px-3 py-3">
                      <LeadStatusBadge status={lead.status} />
                    </td>
                    <td className="px-3 py-3">
                      <CrmBadge status={lead.crm_status} />
                    </td>
                    <td className="px-5 py-3 text-right whitespace-nowrap text-ink-500">
                      {dateTime(lead.created_at)}
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

      {openLeadId ? (
        <LeadDrawer
          leadId={openLeadId}
          onClose={() => setOpenLeadId(null)}
          onChanged={refreshAll}
        />
      ) : null}
    </div>
  );
}
