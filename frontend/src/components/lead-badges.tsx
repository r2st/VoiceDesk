"use client";

import { Badge, type BadgeTone } from "@/components/ui";
import { titleCase } from "@/lib/format";
import type { CrmPushStatus, LeadStatus, LeadTier } from "@/lib/types";

const TIER_TONE: Record<LeadTier, BadgeTone> = {
  hot: "danger",
  warm: "warning",
  cold: "info",
  unqualified: "neutral",
};

const STATUS_TONE: Record<LeadStatus, BadgeTone> = {
  new: "neutral",
  qualified: "success",
  disqualified: "neutral",
  contacted: "info",
  converted: "success",
  lost: "danger",
};

const CRM_TONE: Record<CrmPushStatus, BadgeTone> = {
  sent: "success",
  pending: "warning",
  failed: "danger",
  not_configured: "neutral",
};

const CRM_LABEL: Record<CrmPushStatus, string> = {
  sent: "In CRM",
  pending: "Queued",
  failed: "Not delivered",
  not_configured: "No CRM",
};

export function TierBadge({ tier }: { tier: LeadTier }) {
  return <Badge tone={TIER_TONE[tier] ?? "neutral"}>{titleCase(tier)}</Badge>;
}

export function LeadStatusBadge({ status }: { status: LeadStatus }) {
  return (
    <Badge tone={STATUS_TONE[status] ?? "neutral"}>{titleCase(status)}</Badge>
  );
}

/**
 * Whether the lead reached the tenant's CRM.
 *
 * Shown even when delivery succeeded, because the failure case only means
 * something if the success case is visible too — a salesperson who never sees
 * this column would discover a silent failure when the follow-up never happens.
 */
export function CrmBadge({ status }: { status: CrmPushStatus }) {
  return (
    <Badge tone={CRM_TONE[status] ?? "neutral"}>{CRM_LABEL[status] ?? status}</Badge>
  );
}

/**
 * The score as a bar plus the number.
 *
 * Colour follows the tier rather than the raw score, so a lead that reads
 * "hot" in one column never reads amber in the next: the tenant's thresholds
 * are the only thing that decides which band a number falls in.
 */
export function ScoreBar({ score, tier }: { score: number; tier: LeadTier }) {
  const fill: Record<LeadTier, string> = {
    hot: "bg-rose-500",
    warm: "bg-amber-500",
    cold: "bg-brand-400",
    unqualified: "bg-ink-300",
  };
  return (
    <div className="flex items-center gap-2">
      <div
        className="h-1.5 w-16 overflow-hidden rounded-full bg-ink-100"
        role="img"
        aria-label={`Score ${score} out of 100`}
      >
        <div
          className={`h-full rounded-full ${fill[tier] ?? "bg-ink-300"}`}
          style={{ width: `${Math.max(2, Math.min(100, score))}%` }}
        />
      </div>
      <span className="tnum text-sm font-medium text-ink-900">{score}</span>
    </div>
  );
}
