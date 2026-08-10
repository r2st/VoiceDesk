"use client";

import { useState } from "react";

import { ApiError, api } from "@/lib/api";
import { useSession } from "@/lib/auth-context";
import { useApi } from "@/lib/use-api";
import { duration, languageName, percent, titleCase } from "@/lib/format";
import { Badge, Card, EmptyState, ErrorNotice, Spinner, type BadgeTone } from "@/components/ui";
import type { AgentStatus, VoiceAgent } from "@/lib/types";

const AGENT_TONE: Record<AgentStatus, BadgeTone> = {
  active: "success",
  draft: "neutral",
  paused: "warning",
};

/** The status an admin can flip an agent to next, and the label for that action. */
const NEXT_STATUS: Record<AgentStatus, { status: AgentStatus; label: string }> = {
  active: { status: "paused", label: "Pause" },
  paused: { status: "active", label: "Resume" },
  draft: { status: "active", label: "Activate" },
};

export default function AgentsPage() {
  const { user } = useSession();
  const agents = useApi(() => api.agents.list(), []);
  const canManage = user?.role === "owner" || user?.role === "admin";

  return (
    <div className="mx-auto max-w-5xl">
      <h1 className="text-lg font-semibold text-ink-900">Agents</h1>
      <p className="text-sm text-ink-500">
        The voice agents configured for your business.
      </p>

      {agents.error ? (
        <div className="mt-5">
          <ErrorNotice message={agents.error} onRetry={agents.reload} />
        </div>
      ) : agents.loading && !agents.data ? (
        <div className="mt-6">
          <Spinner label="Loading agents" />
        </div>
      ) : agents.data && agents.data.items.length === 0 ? (
        <Card className="mt-5">
          <EmptyState
            title="No agents yet"
            description="An agent pairs a persona and a conversation flow with a phone number. Create one to start handling calls."
          />
        </Card>
      ) : (
        <div className="mt-5 grid gap-4 md:grid-cols-2">
          {agents.data?.items.map((agent) => (
            <AgentCard key={agent.id} agent={agent} canManage={canManage} />
          ))}
        </div>
      )}
    </div>
  );
}

function AgentCard({
  agent,
  canManage,
}: {
  agent: VoiceAgent;
  canManage: boolean;
}) {
  const [status, setStatus] = useState(agent.status);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const next = NEXT_STATUS[status];

  async function toggleStatus() {
    setBusy(true);
    setError(null);
    try {
      const updated = await api.agents.update(agent.id, { status: next.status });
      setStatus(updated.status);
    } catch (err: unknown) {
      setError(
        err instanceof ApiError ? err.message : "Could not change the agent's status.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold text-ink-900">
            {agent.name}
          </h2>
          <p className="mt-0.5 text-xs text-ink-500">
            {titleCase(agent.use_case)}
          </p>
        </div>
        <Badge tone={AGENT_TONE[status]}>{titleCase(status)}</Badge>
      </div>

      {agent.description ? (
        <p className="mt-3 line-clamp-2 text-sm text-ink-600">
          {agent.description}
        </p>
      ) : null}

      <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
        <Field
          label="Languages"
          value={agent.supported_languages.map(languageName).join(", ")}
        />
        <Field label="Max call" value={duration(agent.max_call_duration_sec)} />
        <Field
          label="Handoff below"
          value={percent(agent.handoff_confidence_threshold, 0)}
        />
        <Field label="Recording" value={agent.recording_enabled ? "On" : "Off"} />
      </dl>

      {agent.greeting ? (
        <p className="mt-4 rounded-lg bg-ink-50 px-3 py-2 text-xs italic text-ink-600">
          “{agent.greeting}”
        </p>
      ) : null}

      {canManage ? (
        <div className="mt-4 flex items-center gap-3 border-t border-ink-100 pt-3">
          <button
            type="button"
            onClick={toggleStatus}
            disabled={busy}
            className="rounded-lg border border-ink-200 px-3 py-1.5 text-xs font-medium text-ink-700 transition hover:bg-ink-50 disabled:opacity-50"
          >
            {busy ? "Saving…" : next.label}
          </button>
          {error ? <span className="text-xs text-rose-700">{error}</span> : null}
        </div>
      ) : null}
    </Card>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-ink-400">{label}</dt>
      <dd className="mt-0.5 font-medium text-ink-700">{value}</dd>
    </div>
  );
}
