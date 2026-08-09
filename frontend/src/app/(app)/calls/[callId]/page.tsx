"use client";

import Link from "next/link";
import { useParams } from "next/navigation";

import { api } from "@/lib/api";
import { useApi } from "@/lib/use-api";
import {
  dateTime,
  duration,
  languageName,
  percent,
  phone,
  rupees,
  titleCase,
} from "@/lib/format";
import {
  ResolutionBadge,
  SentimentBadge,
  StatusBadge,
} from "@/components/call-badges";
import { Card, CardHeader, ErrorNotice, Spinner } from "@/components/ui";
import type { ConversationTurn, SpeakerRole } from "@/lib/types";

const ROLE_STYLE: Record<SpeakerRole, string> = {
  caller: "bg-ink-100 text-ink-900",
  agent: "bg-brand-50 text-ink-900",
  system: "bg-amber-50 text-amber-900",
  human: "bg-emerald-50 text-emerald-900",
};

export default function CallDetailPage() {
  const params = useParams<{ callId: string }>();
  const callId = params.callId;

  const call = useApi(() => api.calls.get(callId), [callId]);

  if (call.error) {
    return (
      <div className="mx-auto max-w-4xl">
        <ErrorNotice message={call.error} onRetry={call.reload} />
      </div>
    );
  }
  if (!call.data) {
    return (
      <div className="mx-auto max-w-4xl">
        <Spinner label="Loading call" />
      </div>
    );
  }

  const data = call.data;

  return (
    <div className="mx-auto max-w-4xl">
      <Link
        href="/calls"
        className="text-sm font-medium text-brand-700 hover:underline"
      >
        ← All calls
      </Link>

      <div className="mt-3 flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold text-ink-900">
          {phone(data.caller_number)}
        </h1>
        <StatusBadge status={data.status} />
        <ResolutionBadge resolution={data.resolution} />
        <SentimentBadge sentiment={data.sentiment} />
      </div>
      <p className="mt-1 text-sm text-ink-500">
        {titleCase(data.direction)} · {dateTime(data.created_at)}
      </p>

      {data.error_message ? (
        <div className="mt-4 rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <strong className="font-semibold">
            {data.error_code ? titleCase(data.error_code) : "Call failed"}:
          </strong>{" "}
          {data.error_message}
        </div>
      ) : null}

      <div className="mt-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Detail label="Duration" value={duration(data.duration_sec)} />
        <Detail
          label="Billed"
          value={`${data.billable_minutes} min · ${rupees(data.cost_paise)}`}
        />
        <Detail label="Language" value={languageName(data.language)} />
        <Detail
          label="Confidence"
          value={
            data.avg_confidence === null
              ? "—"
              : percent(data.avg_confidence, 0)
          }
        />
      </div>

      {data.summary ? (
        <Card className="mt-4">
          <CardHeader title="Summary" subtitle="Generated after the call ended" />
          <p className="px-5 py-4 text-sm leading-relaxed text-ink-700">
            {data.summary}
          </p>
        </Card>
      ) : null}

      <Card className="mt-4">
        <CardHeader
          title="Transcript"
          subtitle={`${data.conversations.length} turn(s)`}
        />
        {data.conversations.length === 0 ? (
          <p className="px-5 py-10 text-center text-sm text-ink-500">
            No transcript was captured for this call.
          </p>
        ) : (
          <ol className="space-y-3 px-5 py-4">
            {data.conversations.map((turn) => (
              <Turn key={turn.id} turn={turn} />
            ))}
          </ol>
        )}
      </Card>

      <Card className="mt-4">
        <CardHeader title="Compliance" subtitle="TRAI checks recorded for this call" />
        <dl className="grid gap-x-8 gap-y-3 px-5 py-4 text-sm sm:grid-cols-2">
          <Row label="DND checked" value={data.dnd_checked ? "Yes" : "No"} />
          <Row
            label="Consent announced"
            value={data.consent_announced ? "Yes" : "No"}
          />
          <Row label="Caller opted out" value={data.opted_out ? "Yes" : "No"} />
          <Row label="Recording stored" value={data.has_recording ? "Yes" : "No"} />
          <Row label="Answered" value={dateTime(data.answered_at)} />
          <Row label="Ended" value={dateTime(data.ended_at)} />
        </dl>
      </Card>
    </div>
  );
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <Card className="px-4 py-3">
      <p className="text-xs font-medium uppercase tracking-wide text-ink-500">
        {label}
      </p>
      <p className="tnum mt-1 text-sm font-semibold text-ink-900">{value}</p>
    </Card>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 border-b border-ink-50 pb-2">
      <dt className="text-ink-500">{label}</dt>
      <dd className="text-ink-900">{value}</dd>
    </div>
  );
}

function Turn({ turn }: { turn: ConversationTurn }) {
  return (
    <li>
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-xs font-semibold uppercase tracking-wide text-ink-500">
          {titleCase(turn.role)}
        </span>
        <span className="text-xs text-ink-400">
          {turn.detected_intent ? `${titleCase(turn.detected_intent)} · ` : ""}
          {turn.latency_ms !== null ? `${turn.latency_ms}ms` : ""}
        </span>
      </div>
      <p
        className={`mt-1 rounded-lg px-3 py-2 text-sm leading-relaxed ${ROLE_STYLE[turn.role]}`}
      >
        {turn.content}
      </p>
    </li>
  );
}
