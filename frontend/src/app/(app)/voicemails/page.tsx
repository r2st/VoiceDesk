"use client";

/**
 * Voicemail triage: messages captured when no agent could take the call.
 *
 * The list is REST and cheap to refetch on an interval, the same pattern the
 * live monitor board uses — voicemails do not arrive often enough to justify
 * their own socket subscription, and a poll keeps the unheard badge honest
 * without extra wiring.
 */

import { useEffect, useRef, useState } from "react";

import { api, ApiError } from "@/lib/api";
import { useApi } from "@/lib/use-api";
import { dateTime, duration, phone } from "@/lib/format";
import { VoicemailStatusBadge } from "@/components/call-badges";
import { Badge, Card, EmptyState, ErrorNotice, Spinner } from "@/components/ui";
import type { Voicemail } from "@/lib/types";

const PAGE_SIZE = 25;
const LIST_POLL_MS = 15000;

export default function VoicemailsPage() {
  const [offset, setOffset] = useState(0);
  const [unheardOnly, setUnheardOnly] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const list = useApi(
    () => api.voicemails.list({ limit: PAGE_SIZE, offset, unheard_only: unheardOnly }),
    [offset, unheardOnly],
  );

  useEffect(() => {
    const timer = setInterval(list.reload, LIST_POLL_MS);
    return () => clearInterval(timer);
  }, [list.reload]);

  const items = list.data?.items ?? [];
  const total = list.data?.total ?? 0;

  useEffect(() => {
    const stillListed = selectedId !== null && items.some((v) => v.id === selectedId);
    if (!stillListed) setSelectedId(items[0]?.id ?? null);
  }, [items, selectedId]);

  function applyFilter(next: () => void) {
    setOffset(0);
    next();
  }

  return (
    <div className="mx-auto max-w-6xl">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-ink-900">Voicemails</h1>
          <p className="text-sm text-ink-500">
            Messages left when no agent could take the call.
          </p>
        </div>
        <label className="flex items-center gap-2 text-sm text-ink-700">
          <input
            type="checkbox"
            checked={unheardOnly}
            onChange={(e) => applyFilter(() => setUnheardOnly(e.target.checked))}
            className="h-4 w-4 rounded border-ink-300"
          />
          Unheard only
        </label>
      </div>

      <div className="mt-5 grid gap-4 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
        <Card className="h-fit">
          {list.error ? (
            <div className="p-5">
              <ErrorNotice message={list.error} onRetry={list.reload} />
            </div>
          ) : list.loading && !list.data ? (
            <div className="px-5 py-8">
              <Spinner label="Loading voicemails" />
            </div>
          ) : items.length === 0 ? (
            <EmptyState
              title="No voicemails"
              description="A caller who reaches a paused or fully-booked agent lands here instead."
            />
          ) : (
            <>
              <ul className="divide-y divide-ink-100">
                {items.map((voicemail) => (
                  <VoicemailRow
                    key={voicemail.id}
                    voicemail={voicemail}
                    selected={voicemail.id === selectedId}
                    onSelect={() => setSelectedId(voicemail.id)}
                  />
                ))}
              </ul>
              <div className="flex items-center justify-between border-t border-ink-100 px-5 py-3 text-sm">
                <span className="text-ink-500">
                  {offset + 1}–{offset + items.length} of {total}
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
                    disabled={offset + items.length >= total}
                    onClick={() => setOffset(offset + PAGE_SIZE)}
                    className="rounded-lg border border-ink-200 px-3 py-1.5 font-medium text-ink-700 disabled:opacity-40"
                  >
                    Next
                  </button>
                </div>
              </div>
            </>
          )}
        </Card>

        {selectedId ? (
          <VoicemailDetail
            key={selectedId}
            voicemailId={selectedId}
            onChange={list.reload}
          />
        ) : (
          <Card>
            <EmptyState
              title="No voicemail selected"
              description="Pick a message from the list to play it back and read its transcript."
            />
          </Card>
        )}
      </div>
    </div>
  );
}

function VoicemailRow({
  voicemail,
  selected,
  onSelect,
}: {
  voicemail: Voicemail;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        aria-current={selected}
        className={`w-full px-5 py-3 text-left transition ${
          selected ? "bg-brand-50" : "hover:bg-ink-50"
        }`}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-sm font-medium text-ink-900">
            {phone(voicemail.caller_number)}
          </span>
          {voicemail.is_unheard ? <Badge tone="info">New</Badge> : null}
        </div>
        <div className="mt-1 flex items-center gap-2 text-xs text-ink-500">
          <span>{dateTime(voicemail.created_at)}</span>
          <span>·</span>
          <span className="tnum">{duration(voicemail.duration_sec)}</span>
        </div>
      </button>
    </li>
  );
}

function VoicemailDetail({
  voicemailId,
  onChange,
}: {
  voicemailId: string;
  onChange: () => void;
}) {
  const detail = useApi(() => api.voicemails.get(voicemailId), [voicemailId]);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const voicemail = detail.data;

  useEffect(() => {
    if (voicemail && voicemail.listened_at === null) {
      void api.voicemails.listen(voicemailId).then(() => {
        detail.reload();
        onChange();
      });
    }
    // Marking listened is a one-way transition triggered by opening the
    // detail pane; it should not re-fire when `detail`/`onChange` change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [voicemailId, voicemail?.listened_at]);

  async function remove() {
    setBusy(true);
    setActionError(null);
    try {
      await api.voicemails.remove(voicemailId);
      onChange();
    } catch (err) {
      setActionError(
        err instanceof ApiError ? err.message : "Could not delete this voicemail.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (detail.error) {
    return (
      <Card className="p-5">
        <ErrorNotice message={detail.error} onRetry={detail.reload} />
      </Card>
    );
  }
  if (!voicemail) {
    return (
      <Card className="p-5">
        <Spinner label="Loading voicemail" />
      </Card>
    );
  }

  return (
    <Card>
      <div className="flex items-start justify-between gap-4 border-b border-ink-100 px-5 py-4">
        <div>
          <h2 className="text-sm font-semibold text-ink-900">
            {phone(voicemail.caller_number)}
          </h2>
          <p className="mt-0.5 text-xs text-ink-500">
            {dateTime(voicemail.created_at)} · {duration(voicemail.duration_sec)}
          </p>
        </div>
        <VoicemailStatusBadge status={voicemail.status} />
      </div>

      <div className="px-5 py-4">
        <VoicemailPlayer voicemailId={voicemailId} />
      </div>

      <div className="border-t border-ink-100 px-5 py-4">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-500">
          Transcript
        </h3>
        <p className="mt-2 text-sm text-ink-700">
          {voicemail.transcript ?? (
            <span className="text-ink-400">
              {voicemail.status === "failed"
                ? "Transcription failed for this message."
                : "Not transcribed yet."}
            </span>
          )}
        </p>
      </div>

      {actionError ? (
        <p className="px-5 pb-2 text-sm text-rose-700">{actionError}</p>
      ) : null}

      <div className="flex justify-end border-t border-ink-100 px-5 py-3">
        <button
          type="button"
          onClick={remove}
          disabled={busy}
          className="rounded-lg border border-rose-200 px-3 py-1.5 text-sm font-medium text-rose-700 transition hover:bg-rose-50 disabled:opacity-50"
        >
          Delete
        </button>
      </div>
    </Card>
  );
}

/**
 * Fetched lazily on request, mirroring the call recording player: the audio
 * is decrypted server-side on every request, so there is no reason to pay
 * that cost for a voicemail the operator never plays.
 */
function VoicemailPlayer({ voicemailId }: { voicemailId: string }) {
  const [status, setStatus] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [error, setError] = useState<string | null>(null);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const objectUrlRef = useRef<string | null>(null);

  useEffect(() => {
    return () => {
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
    };
  }, []);

  async function load() {
    setStatus("loading");
    setError(null);
    try {
      const blob = await api.voicemails.stream(voicemailId);
      const url = URL.createObjectURL(blob);
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
      objectUrlRef.current = url;
      setAudioUrl(url);
      setStatus("ready");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load the voicemail.");
      setStatus("error");
    }
  }

  if (status === "ready" && audioUrl) {
    // eslint-disable-next-line jsx-a11y/media-has-caption
    return <audio className="w-full" controls autoPlay src={audioUrl} />;
  }

  return (
    <div className="flex items-center gap-3">
      <button
        type="button"
        onClick={load}
        disabled={status === "loading"}
        className="rounded-md border border-ink-200 bg-white px-3 py-1.5 text-sm font-medium text-ink-700 hover:bg-ink-50 disabled:opacity-60"
      >
        {status === "loading" ? "Decrypting…" : "Play voicemail"}
      </button>
      {error ? <span className="text-sm text-rose-700">{error}</span> : null}
    </div>
  );
}
