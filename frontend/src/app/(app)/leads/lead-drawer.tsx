"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { ApiError, api } from "@/lib/api";
import { useApi } from "@/lib/use-api";
import { dateTime, languageName, phone, titleCase } from "@/lib/format";
import { CrmBadge, LeadStatusBadge, ScoreBar, TierBadge } from "@/components/lead-badges";
import { ErrorNotice, Spinner } from "@/components/ui";
import type { DimensionScore, LeadStatus } from "@/lib/types";

/**
 * Statuses a salesperson may set.
 *
 * `qualified` and `disqualified` are deliberately absent: they are outcomes of
 * scoring, and the API rejects them. Offering them here would put a 422 behind
 * a button that looks like every other one.
 */
const NEXT_STATUSES: LeadStatus[] = ["contacted", "converted", "lost"];

const DIMENSION_LABEL: Record<DimensionScore["dimension"], string> = {
  budget: "Budget",
  authority: "Authority",
  need: "Need",
  timeline: "Timeline",
};

export function LeadDrawer({
  leadId,
  onClose,
  onChanged,
}: {
  leadId: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const lead = useApi(() => api.leads.get(leadId), [leadId]);
  const [saving, setSaving] = useState<LeadStatus | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // Escape closes, matching how every other overlay on the web behaves.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function setStatus(status: LeadStatus) {
    setSaving(status);
    setActionError(null);
    try {
      await api.leads.setStatus(leadId, status);
      lead.reload();
      onChanged();
    } catch (err: unknown) {
      setActionError(
        err instanceof ApiError ? err.message : "Could not update this lead.",
      );
    } finally {
      setSaving(null);
    }
  }

  const data = lead.data;

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      {/* Click-outside-to-close. Hidden from assistive tech and from the tab
          order: the header button and Escape already offer the same action,
          and announcing a second "Close" is only noise. */}
      <div
        aria-hidden="true"
        onClick={onClose}
        className="absolute inset-0 bg-ink-900/20"
      />

      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Lead detail"
        className="relative flex h-full w-full max-w-lg flex-col overflow-y-auto border-l border-ink-200 bg-white shadow-xl"
      >
        {/* Sticky so the contact stays visible while a long BANT breakdown
            scrolls — the name is the context for everything below it. */}
        <header className="sticky top-0 z-10 flex items-start justify-between gap-4 border-b border-ink-100 bg-white px-5 py-4">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold text-ink-900">
              {data?.contact_name ?? "Lead"}
            </h2>
            {data ? (
              <p className="tnum mt-0.5 text-xs text-ink-500">
                {phone(data.contact_phone)}
                {data.company ? ` · ${data.company}` : ""}
              </p>
            ) : null}
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-ink-200 px-2.5 py-1 text-xs font-medium text-ink-700 hover:bg-ink-50"
          >
            Close
          </button>
        </header>

        {lead.error ? (
          <div className="p-5">
            <ErrorNotice message={lead.error} onRetry={lead.reload} />
          </div>
        ) : !data ? (
          <div className="p-5">
            <Spinner label="Loading lead" />
          </div>
        ) : (
          <div className="flex-1 px-5 py-4">
            <div className="flex flex-wrap items-center gap-2">
              <ScoreBar score={data.score} tier={data.tier} />
              <TierBadge tier={data.tier} />
              <LeadStatusBadge status={data.status} />
              <CrmBadge status={data.crm_status} />
            </div>

            {data.crm_status === "failed" && data.crm_error ? (
              <p className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-800">
                This lead has not reached your CRM: {data.crm_error}
              </p>
            ) : null}

            <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
              <Field label="Captured" value={dateTime(data.created_at)} />
              <Field label="Source" value={titleCase(data.source)} />
              <Field label="Language" value={languageName(data.language)} />
              <Field
                label="Interest"
                value={data.interest ?? "—"}
              />
            </dl>

            <h3 className="mt-6 text-xs font-semibold uppercase tracking-wide text-ink-500">
              How this score was reached
            </h3>
            {/* The caller's own words sit next to every number: a salesperson
                looking at a 40/100 should be able to argue with it rather than
                only trust it. */}
            <ul className="mt-2 space-y-3">
              {data.breakdown.map((row) => (
                <li
                  key={row.dimension}
                  className="rounded-lg border border-ink-100 px-3 py-2.5"
                >
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-sm font-medium text-ink-900">
                      {DIMENSION_LABEL[row.dimension]}
                    </span>
                    <span className="tnum text-sm text-ink-600">
                      {row.percent}%
                    </span>
                  </div>
                  <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-ink-100">
                    <div
                      className="h-full rounded-full bg-brand-500"
                      style={{ width: `${Math.max(2, row.percent)}%` }}
                    />
                  </div>
                  <p className="mt-1.5 text-xs text-ink-500">{row.reason}</p>
                  {row.answer ? (
                    <p className="mt-1.5 border-l-2 border-ink-200 pl-2 text-xs italic text-ink-600">
                      “{row.answer}”
                    </p>
                  ) : (
                    <p className="mt-1.5 text-xs text-ink-400">
                      The caller was not asked, or did not answer.
                    </p>
                  )}
                </li>
              ))}
            </ul>

            {data.notes ? (
              <>
                <h3 className="mt-6 text-xs font-semibold uppercase tracking-wide text-ink-500">
                  Notes
                </h3>
                <p className="mt-2 whitespace-pre-wrap text-sm text-ink-700">
                  {data.notes}
                </p>
              </>
            ) : null}

            {data.call_id ? (
              <Link
                href={`/calls/${data.call_id}`}
                className="mt-6 inline-block text-sm font-medium text-brand-700 hover:underline"
              >
                Open the qualification call →
              </Link>
            ) : null}
          </div>
        )}

        {data ? (
          <footer className="sticky bottom-0 border-t border-ink-100 bg-white px-5 py-3">
            {actionError ? (
              <p className="mb-2 text-xs text-rose-700">{actionError}</p>
            ) : null}
            <div className="flex flex-wrap gap-2">
              {NEXT_STATUSES.map((status) => (
                <button
                  key={status}
                  type="button"
                  disabled={saving !== null || data.status === status}
                  onClick={() => void setStatus(status)}
                  className="rounded-lg border border-ink-200 px-3 py-1.5 text-sm font-medium text-ink-700 transition hover:bg-ink-50 disabled:opacity-40"
                >
                  {saving === status ? "Saving…" : `Mark ${status}`}
                </button>
              ))}
            </div>
          </footer>
        ) : null}
      </aside>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-ink-500">{label}</dt>
      <dd className="truncate text-ink-800">{value}</dd>
    </div>
  );
}
