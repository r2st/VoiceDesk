"use client";

import { Badge, type BadgeTone } from "@/components/ui";
import { titleCase } from "@/lib/format";
import type {
  CallResolution,
  CallStatus,
  QualityGrade,
  Sentiment,
  VoicemailStatus,
} from "@/lib/types";

const STATUS_TONE: Record<CallStatus, BadgeTone> = {
  queued: "neutral",
  ringing: "info",
  in_progress: "info",
  completed: "success",
  no_answer: "warning",
  busy: "warning",
  failed: "danger",
  cancelled: "neutral",
  blocked_dnd: "danger",
  blocked_calling_hours: "danger",
};

const RESOLUTION_TONE: Record<CallResolution, BadgeTone> = {
  resolved: "success",
  unresolved: "warning",
  escalated: "danger",
  handed_off: "info",
  pending: "neutral",
};

const SENTIMENT_TONE: Record<Sentiment, BadgeTone> = {
  positive: "success",
  neutral: "neutral",
  negative: "danger",
};

/** `blocked_calling_hours` is too long for a table cell at its full width. */
const STATUS_LABEL: Partial<Record<CallStatus, string>> = {
  blocked_calling_hours: "Outside hours",
  blocked_dnd: "DND blocked",
};

export function StatusBadge({ status }: { status: CallStatus }) {
  return (
    <Badge tone={STATUS_TONE[status] ?? "neutral"}>
      {STATUS_LABEL[status] ?? titleCase(status)}
    </Badge>
  );
}

export function ResolutionBadge({ resolution }: { resolution: CallResolution }) {
  return (
    <Badge tone={RESOLUTION_TONE[resolution] ?? "neutral"}>
      {titleCase(resolution)}
    </Badge>
  );
}

export function SentimentBadge({ sentiment }: { sentiment: Sentiment | null }) {
  if (!sentiment) return <span className="text-ink-400">—</span>;
  return <Badge tone={SENTIMENT_TONE[sentiment]}>{titleCase(sentiment)}</Badge>;
}

const QUALITY_TONE: Record<QualityGrade, BadgeTone> = {
  good: "success",
  fair: "warning",
  poor: "danger",
};

export function QualityBadge({ grade }: { grade: QualityGrade | null }) {
  if (!grade) return <span className="text-ink-400">—</span>;
  return <Badge tone={QUALITY_TONE[grade]}>{titleCase(grade)}</Badge>;
}

const VOICEMAIL_STATUS_TONE: Record<VoicemailStatus, BadgeTone> = {
  pending: "neutral",
  transcribing: "info",
  transcribed: "success",
  failed: "danger",
};

export function VoicemailStatusBadge({ status }: { status: VoicemailStatus }) {
  return <Badge tone={VOICEMAIL_STATUS_TONE[status]}>{titleCase(status)}</Badge>;
}
