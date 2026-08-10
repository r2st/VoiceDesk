"use client";

import { useState } from "react";

import { ApiError, api } from "@/lib/api";
import { useSession } from "@/lib/auth-context";
import { useApi } from "@/lib/use-api";
import { dateTime, phone, rupees, titleCase } from "@/lib/format";
import { Badge, Card, EmptyState, ErrorNotice, Spinner, type BadgeTone } from "@/components/ui";
import type { PhoneNumber, VoiceAgent } from "@/lib/types";

const NUMBER_TONE: Record<PhoneNumber["status"], BadgeTone> = {
  active: "success",
  provisioning: "info",
  released: "neutral",
  failed: "danger",
};

export default function NumbersPage() {
  const { user } = useSession();
  const numbers = useApi(() => api.phoneNumbers.list(), []);
  const agents = useApi(() => api.agents.list(), []);
  const canManage = user?.role === "owner" || user?.role === "admin";

  const agentList = agents.data?.items ?? [];
  const agentNames = new Map(agentList.map((agent) => [agent.id, agent.name]));

  return (
    <div className="mx-auto max-w-5xl">
      <h1 className="text-lg font-semibold text-ink-900">Phone numbers</h1>
      <p className="text-sm text-ink-500">
        Numbers provisioned from your telephony provider and the agents that
        answer them.
      </p>

      <Card className="mt-5 overflow-hidden">
        {numbers.error ? (
          <div className="p-5">
            <ErrorNotice message={numbers.error} onRetry={numbers.reload} />
          </div>
        ) : numbers.loading && !numbers.data ? (
          <div className="p-5">
            <Spinner label="Loading numbers" />
          </div>
        ) : numbers.data && numbers.data.items.length === 0 ? (
          <EmptyState
            title="No numbers provisioned"
            description="Provision a number to give your agents a line that customers can call."
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
              <thead>
                <tr className="border-b border-ink-100 text-left text-xs uppercase tracking-wide text-ink-500">
                  <th className="px-5 py-2.5 font-medium">Number</th>
                  <th className="px-3 py-2.5 font-medium">Agent</th>
                  <th className="px-3 py-2.5 font-medium">Status</th>
                  <th className="px-3 py-2.5 font-medium">Direction</th>
                  <th className="px-3 py-2.5 font-medium">Region</th>
                  <th className="px-3 py-2.5 text-right font-medium">Rent</th>
                  <th className="px-5 py-2.5 text-right font-medium">Added</th>
                </tr>
              </thead>
              <tbody>
                {numbers.data?.items.map((num) => (
                  <NumberRow
                    key={num.id}
                    number={num}
                    agentName={
                      num.agent_id
                        ? (agentNames.get(num.agent_id) ?? "Unknown agent")
                        : null
                    }
                    agents={agentList}
                    canManage={canManage}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

const UNASSIGNED = "";

function NumberRow({
  number,
  agentName,
  agents,
  canManage,
}: {
  number: PhoneNumber;
  agentName: string | null;
  agents: VoiceAgent[];
  canManage: boolean;
}) {
  const [agentId, setAgentId] = useState(number.agent_id ?? UNASSIGNED);
  const [resolvedName, setResolvedName] = useState(agentName);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const editable = canManage && number.status === "active";

  async function onChange(nextAgentId: string) {
    setBusy(true);
    setError(null);
    try {
      const updated =
        nextAgentId === UNASSIGNED
          ? await api.phoneNumbers.unassign(number.id)
          : await api.phoneNumbers.assign(number.id, nextAgentId);
      setAgentId(updated.agent_id ?? UNASSIGNED);
      setResolvedName(
        agents.find((agent) => agent.id === updated.agent_id)?.name ?? null,
      );
    } catch (err: unknown) {
      setError(
        err instanceof ApiError ? err.message : "Could not change the agent.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <tr className="border-b border-ink-50 last:border-0">
      <td className="px-5 py-3 font-medium whitespace-nowrap text-ink-900">
        {phone(number.number)}
      </td>
      <td className="px-3 py-3 text-ink-600">
        {editable ? (
          <div>
            <select
              aria-label={`Agent for ${phone(number.number)}`}
              value={agentId}
              disabled={busy}
              onChange={(event) => {
                setAgentId(event.target.value);
                void onChange(event.target.value);
              }}
              className="w-full max-w-[10rem] rounded-md border border-ink-200 bg-white px-2 py-1 text-xs outline-none focus:border-brand-500 disabled:opacity-50"
            >
              <option value={UNASSIGNED}>Unassigned</option>
              {agents.map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agent.name}
                </option>
              ))}
            </select>
            {error ? <p className="mt-1 text-xs text-rose-700">{error}</p> : null}
          </div>
        ) : (
          (resolvedName ?? "Unassigned")
        )}
      </td>
      <td className="px-3 py-3">
        <Badge tone={NUMBER_TONE[number.status]}>
          {titleCase(number.status)}
        </Badge>
      </td>
      <td className="px-3 py-3 text-xs text-ink-500">
        {[
          number.inbound_enabled ? "In" : null,
          number.outbound_enabled ? "Out" : null,
        ]
          .filter(Boolean)
          .join(" / ") || "Disabled"}
      </td>
      <td className="px-3 py-3 text-ink-600">{number.region ?? "—"}</td>
      <td className="tnum px-3 py-3 text-right text-ink-600">
        {rupees(number.monthly_rent_paise)}/mo
      </td>
      <td className="px-5 py-3 text-right whitespace-nowrap text-ink-500">
        {dateTime(number.created_at)}
      </td>
    </tr>
  );
}
