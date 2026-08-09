"use client";

/**
 * Live call monitoring (design doc §4.3).
 *
 * The board on the left is REST — it is a list, and a list is cheap to refetch.
 * The pane on the right is the WebSocket: it opens with a snapshot of the
 * transcript so far and then appends each turn as it is spoken, so a supervisor
 * reading a call never sees a blank pane or has to poll for the next line.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";

import { SentimentBadge, StatusBadge } from "@/components/call-badges";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorNotice,
  Spinner,
} from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { duration, languageName, phone, titleCase } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { useLiveStream, type StreamStatus } from "@/lib/use-live-stream";
import type {
  ConversationTurn,
  LiveCall,
  SpeakerRole,
  StreamFrame,
  Takeover,
} from "@/lib/types";

const ROLE_STYLE: Record<SpeakerRole, string> = {
  caller: "bg-ink-100 text-ink-900",
  agent: "bg-brand-50 text-ink-900",
  system: "bg-amber-50 text-amber-900",
  human: "bg-emerald-50 text-emerald-900",
};

/** How often the board refetches. Turns arrive over the socket, not from this. */
const BOARD_POLL_MS = 10000;

export default function MonitorPage() {
  const board = useApi(() => api.monitor.live(), []);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // The board list is small and cheap; a slow poll keeps calls appearing and
  // disappearing without every row needing its own subscription.
  useEffect(() => {
    const timer = setInterval(board.reload, BOARD_POLL_MS);
    return () => clearInterval(timer);
  }, [board.reload]);

  const calls = board.data ?? [];

  // A call that ends drops off the board; keep the pane pointed at something
  // real rather than at a call id that no longer exists.
  useEffect(() => {
    const stillLive =
      selectedId !== null && calls.some((c) => c.call_id === selectedId);
    if (!stillLive) setSelectedId(calls[0]?.call_id ?? null);
  }, [calls, selectedId]);

  return (
    <div className="mx-auto max-w-7xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-ink-900">Live calls</h1>
          <p className="mt-0.5 text-sm text-ink-500">
            Watch a call as it happens, and step in when the agent needs help.
          </p>
        </div>
        <span className="text-sm text-ink-500">
          {calls.length} in progress
        </span>
      </div>

      {board.error ? (
        <div className="mt-4">
          <ErrorNotice message={board.error} onRetry={board.reload} />
        </div>
      ) : null}

      <div className="mt-5 grid gap-4 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
        <Card className="h-fit">
          <CardHeader title="On the board" subtitle="Refreshes every 10 seconds" />
          {board.loading && !board.data ? (
            <div className="px-5 py-8">
              <Spinner label="Loading live calls" />
            </div>
          ) : calls.length === 0 ? (
            <EmptyState
              title="Nothing live right now"
              description="Calls appear here the moment they start ringing. Finished calls move to the call log."
            />
          ) : (
            <ul className="divide-y divide-ink-100">
              {calls.map((call) => (
                <BoardRow
                  key={call.call_id}
                  call={call}
                  selected={call.call_id === selectedId}
                  onSelect={() => setSelectedId(call.call_id)}
                />
              ))}
            </ul>
          )}
        </Card>

        {selectedId ? (
          <CallPane
            key={selectedId}
            callId={selectedId}
            onControlChange={board.reload}
          />
        ) : (
          <Card>
            <EmptyState
              title="No call selected"
              description="Pick a call from the board to follow its transcript live."
            />
          </Card>
        )}
      </div>
    </div>
  );
}

function BoardRow({
  call,
  selected,
  onSelect,
}: {
  call: LiveCall;
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
            {phone(call.caller_number)}
          </span>
          <StatusBadge status={call.status} />
        </div>
        <div className="mt-1 flex items-center gap-2 text-xs text-ink-500">
          <span className="tnum">{duration(call.elapsed_sec)}</span>
          <span>·</span>
          <span>{titleCase(call.direction)}</span>
          <span>·</span>
          <span className="tnum">{call.turn_count} turns</span>
          {call.takeover ? (
            <Badge tone="warning">Human</Badge>
          ) : null}
        </div>
        {call.last_utterance ? (
          <p className="mt-1.5 truncate text-xs text-ink-600">
            <span className="font-medium">
              {call.last_speaker ? titleCase(call.last_speaker) : "—"}:
            </span>{" "}
            {call.last_utterance}
          </p>
        ) : null}
      </button>
    </li>
  );
}

function CallPane({
  callId,
  onControlChange,
}: {
  callId: string;
  onControlChange: () => void;
}) {
  const snapshot = useApi(() => api.monitor.snapshot(callId), [callId]);
  const [turns, setTurns] = useState<TranscriptLine[]>([]);
  const [takeover, setTakeover] = useState<Takeover | null>(null);
  const [ended, setEnded] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState("");
  const transcriptRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!snapshot.data) return;
    setTurns(snapshot.data.turns.map(toLine));
    setTakeover(snapshot.data.takeover);
    setEnded(isTerminal(snapshot.data.call.status));
  }, [snapshot.data]);

  const onFrame = useCallback((frame: StreamFrame) => {
    if (frame.type === "ping") return;

    if (frame.type === "snapshot") {
      setTurns(frame.data.turns.map(toLine));
      setTakeover(frame.data.takeover);
      return;
    }

    switch (frame.type) {
      case "transcript.turn":
        setTurns((current) => appendTurn(current, frame.data));
        break;
      case "takeover.started":
        setTakeover((current) => current ?? pendingTakeover(frame.data));
        break;
      case "takeover.ended":
        setTakeover(null);
        break;
      case "call.ended":
        setEnded(true);
        break;
    }
  }, []);

  const { status } = useLiveStream(onFrame, { callId });

  // Follow the conversation. Only the transcript box scrolls, so a supervisor
  // reading back through earlier turns is not yanked to the bottom by the page.
  useEffect(() => {
    const box = transcriptRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [turns.length]);

  const held = takeover !== null;

  const run = useCallback(
    async (action: () => Promise<unknown>) => {
      setBusy(true);
      setActionError(null);
      try {
        await action();
        onControlChange();
      } catch (error) {
        setActionError(
          error instanceof ApiError
            ? error.message
            : "That did not go through. Try again.",
        );
      } finally {
        setBusy(false);
      }
    },
    [onControlChange],
  );

  const takeOver = () =>
    run(async () => setTakeover(await api.monitor.takeOver(callId)));

  const release = () =>
    run(async () => {
      await api.monitor.release(callId);
      setTakeover(null);
    });

  const say = (event: React.FormEvent) => {
    event.preventDefault();
    const text = draft.trim();
    if (!text) return;
    setDraft("");
    void run(async () => {
      const turn = await api.monitor.say(callId, text);
      // The socket will deliver this turn too; `appendTurn` is keyed on the
      // turn index so the echo replaces this line rather than duplicating it.
      setTurns((current) => appendTurn(current, toLine(turn)));
    });
  };

  if (snapshot.error) {
    return (
      <Card className="p-5">
        <ErrorNotice message={snapshot.error} onRetry={snapshot.reload} />
      </Card>
    );
  }
  if (!snapshot.data) {
    return (
      <Card className="p-5">
        <Spinner label="Opening call" />
      </Card>
    );
  }

  const call = snapshot.data.call;

  return (
    <Card>
      <CardHeader
        title={phone(call.caller_number)}
        subtitle={`${titleCase(call.direction)} · ${languageName(call.language)}`}
        action={
          <div className="flex items-center gap-2">
            <ConnectionDot status={status} />
            <StatusBadge status={call.status} />
            <SentimentBadge sentiment={call.sentiment} />
          </div>
        }
      />

      {held ? (
        <p className="border-b border-amber-200 bg-amber-50 px-5 py-2.5 text-sm text-amber-900">
          <strong className="font-semibold">You have this call.</strong> The AI
          agent is silent — the caller hears only what you send.
        </p>
      ) : null}

      {ended ? (
        <p className="border-b border-ink-200 bg-ink-50 px-5 py-2.5 text-sm text-ink-700">
          This call has ended.{" "}
          <Link
            href={`/calls/${callId}`}
            className="font-medium text-brand-700 hover:underline"
          >
            Open the full record
          </Link>
        </p>
      ) : null}

      <div
        ref={transcriptRef}
        className="max-h-[26rem] space-y-3 overflow-y-auto px-5 py-4"
      >
        {turns.length === 0 ? (
          <p className="py-10 text-center text-sm text-ink-500">
            Waiting for the first turn…
          </p>
        ) : (
          turns.map((turn) => <TurnBubble key={turn.turn_index} turn={turn} />)
        )}
      </div>

      {actionError ? (
        <p className="px-5 pb-2 text-sm text-rose-700">{actionError}</p>
      ) : null}

      <div className="border-t border-ink-100 px-5 py-4">
        {ended ? null : held ? (
          <form onSubmit={say} className="flex gap-2">
            <input
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="Type what the caller should hear…"
              aria-label="Reply to the caller"
              className="min-w-0 flex-1 rounded-lg border border-ink-200 px-3 py-2 text-sm outline-none focus:border-brand-500"
            />
            <button
              type="submit"
              disabled={busy || draft.trim().length === 0}
              className="rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-brand-700 disabled:opacity-50"
            >
              Send
            </button>
            <button
              type="button"
              onClick={release}
              disabled={busy}
              className="rounded-lg border border-ink-200 px-3 py-2 text-sm font-medium text-ink-700 transition hover:bg-ink-50 disabled:opacity-50"
            >
              Hand back to AI
            </button>
          </form>
        ) : (
          <button
            type="button"
            onClick={takeOver}
            disabled={busy}
            className="rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-brand-700 disabled:opacity-50"
          >
            Take over this call
          </button>
        )}
      </div>
    </Card>
  );
}

function ConnectionDot({ status }: { status: StreamStatus }) {
  const label: Record<StreamStatus, string> = {
    connecting: "Connecting",
    open: "Live",
    closed: "Reconnecting",
    unauthorized: "Sign in again",
  };
  const tone: Record<StreamStatus, string> = {
    connecting: "bg-amber-500",
    open: "bg-emerald-500",
    closed: "bg-amber-500",
    unauthorized: "bg-rose-500",
  };
  return (
    <span className="flex items-center gap-1.5 text-xs text-ink-500">
      <span className={`h-2 w-2 rounded-full ${tone[status]}`} />
      {label[status]}
    </span>
  );
}

function TurnBubble({ turn }: { turn: TranscriptLine }) {
  return (
    <div>
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-xs font-semibold uppercase tracking-wide text-ink-500">
          {turn.role === "human" ? "You" : titleCase(turn.role)}
        </span>
        <span className="text-xs text-ink-400">
          {turn.confidence !== null && turn.confidence < 0.7
            ? `low confidence · ${Math.round(turn.confidence * 100)}%`
            : ""}
        </span>
      </div>
      <p
        className={`mt-1 rounded-lg px-3 py-2 text-sm leading-relaxed ${ROLE_STYLE[turn.role]}`}
      >
        {turn.content}
      </p>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Transcript state
// --------------------------------------------------------------------------- //
interface TranscriptLine {
  turn_index: number;
  role: SpeakerRole;
  content: string;
  confidence: number | null;
}

function toLine(turn: ConversationTurn): TranscriptLine {
  return {
    turn_index: turn.turn_index,
    role: turn.role,
    content: turn.content,
    confidence: turn.confidence,
  };
}

/**
 * Insert a turn, keyed on its index.
 *
 * The same turn can arrive twice — once as the response to the supervisor's
 * own POST and once over the socket — and out of order after a reconnect
 * replays a snapshot. Keying on the index makes both cases idempotent.
 */
function appendTurn(
  current: TranscriptLine[],
  incoming: TranscriptLine | Record<string, unknown>,
): TranscriptLine[] {
  const line = normaliseLine(incoming);
  if (!line) return current;

  const existing = current.findIndex((t) => t.turn_index === line.turn_index);
  if (existing >= 0) {
    const next = [...current];
    next[existing] = line;
    return next;
  }
  return [...current, line].sort((a, b) => a.turn_index - b.turn_index);
}

function normaliseLine(
  value: TranscriptLine | Record<string, unknown>,
): TranscriptLine | null {
  const index = value.turn_index;
  const role = value.role;
  const content = value.content;
  if (typeof index !== "number" || typeof role !== "string" || typeof content !== "string") {
    return null;
  }
  const confidence = value.confidence;
  return {
    turn_index: index,
    role: role as SpeakerRole,
    content,
    confidence: typeof confidence === "number" ? confidence : null,
  };
}

/**
 * A `takeover.started` event carries who took the call but not the full row.
 * This is enough to flip the pane into supervised mode; the authoritative row
 * arrives with the next snapshot or the supervisor's own takeover response.
 */
function pendingTakeover(data: Record<string, unknown>): Takeover | null {
  const id = data.takeover_id;
  const supervisor = data.supervisor_user_id;
  if (typeof id !== "string" || typeof supervisor !== "string") return null;
  return {
    id,
    business_id: String(data.business_id ?? ""),
    call_id: String(data.call_id ?? ""),
    supervisor_user_id: supervisor,
    reason: typeof data.reason === "string" ? data.reason : null,
    started_at: new Date().toISOString(),
    ended_at: null,
    turns_spoken: 0,
    returned_to_ai: false,
  };
}

const TERMINAL_STATUSES = new Set([
  "completed",
  "no_answer",
  "busy",
  "failed",
  "cancelled",
  "blocked_dnd",
  "blocked_calling_hours",
]);

function isTerminal(status: string): boolean {
  return TERMINAL_STATUSES.has(status);
}
