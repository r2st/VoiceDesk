"use client";

import { api } from "@/lib/api";
import { useApi } from "@/lib/use-api";
import { dateTime, phone, rupees, titleCase } from "@/lib/format";
import { Badge, Card, EmptyState, ErrorNotice, Spinner, type BadgeTone } from "@/components/ui";
import type { PhoneNumber } from "@/lib/types";

const NUMBER_TONE: Record<PhoneNumber["status"], BadgeTone> = {
  active: "success",
  provisioning: "info",
  released: "neutral",
  failed: "danger",
};

export default function NumbersPage() {
  const numbers = useApi(() => api.phoneNumbers.list(), []);
  const agents = useApi(() => api.agents.list(), []);

  const agentNames = new Map(
    (agents.data?.items ?? []).map((agent) => [agent.id, agent.name]),
  );

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
                  <tr
                    key={num.id}
                    className="border-b border-ink-50 last:border-0"
                  >
                    <td className="px-5 py-3 font-medium whitespace-nowrap text-ink-900">
                      {phone(num.number)}
                    </td>
                    <td className="px-3 py-3 text-ink-600">
                      {num.agent_id
                        ? (agentNames.get(num.agent_id) ?? "Unknown agent")
                        : "Unassigned"}
                    </td>
                    <td className="px-3 py-3">
                      <Badge tone={NUMBER_TONE[num.status]}>
                        {titleCase(num.status)}
                      </Badge>
                    </td>
                    <td className="px-3 py-3 text-xs text-ink-500">
                      {[
                        num.inbound_enabled ? "In" : null,
                        num.outbound_enabled ? "Out" : null,
                      ]
                        .filter(Boolean)
                        .join(" / ") || "Disabled"}
                    </td>
                    <td className="px-3 py-3 text-ink-600">
                      {num.region ?? "—"}
                    </td>
                    <td className="tnum px-3 py-3 text-right text-ink-600">
                      {rupees(num.monthly_rent_paise)}/mo
                    </td>
                    <td className="px-5 py-3 text-right whitespace-nowrap text-ink-500">
                      {dateTime(num.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
