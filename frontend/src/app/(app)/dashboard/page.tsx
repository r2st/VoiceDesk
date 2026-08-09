"use client";

import { useState } from "react";

import { api } from "@/lib/api";
import { useApi } from "@/lib/use-api";
import {
  duration,
  isoDaysAgo,
  languageName,
  number,
  percent,
  rupees,
  signedPercent,
  titleCase,
} from "@/lib/format";
import { BarChart, DistributionBars } from "@/components/chart";
import {
  Card,
  CardHeader,
  ErrorNotice,
  Spinner,
  StatCard,
} from "@/components/ui";

const RANGES = [7, 30, 90] as const;

export default function DashboardPage() {
  const [days, setDays] = useState<number>(30);

  const summary = useApi(() => api.analytics.dashboard(days), [days]);
  const analytics = useApi(
    () =>
      api.analytics.calls({
        date_from: isoDaysAgo(days - 1),
        date_to: isoDaysAgo(0),
      }),
    [days],
  );
  const leaderboard = useApi(() => api.analytics.agents(days), [days]);

  return (
    <div className="mx-auto max-w-6xl">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-ink-900">Overview</h1>
          <p className="text-sm text-ink-500">
            Call volume, resolution and spend across your agents.
          </p>
        </div>
        <div className="flex rounded-lg border border-ink-200 bg-white p-0.5">
          {RANGES.map((range) => (
            <button
              key={range}
              type="button"
              onClick={() => setDays(range)}
              className={`rounded-md px-3 py-1.5 text-sm font-medium transition ${
                days === range
                  ? "bg-brand-600 text-white"
                  : "text-ink-600 hover:text-ink-900"
              }`}
            >
              {range}d
            </button>
          ))}
        </div>
      </div>

      {summary.error ? (
        <div className="mt-5">
          <ErrorNotice message={summary.error} onRetry={summary.reload} />
        </div>
      ) : null}

      {summary.loading && !summary.data ? (
        <div className="mt-8">
          <Spinner label="Loading metrics" />
        </div>
      ) : null}

      {summary.data ? (
        <>
          <div className="mt-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <StatCard
              label="Total calls"
              value={number(summary.data.total_calls)}
              delta={{
                value: signedPercent(summary.data.deltas.total_calls),
                positive: summary.data.deltas.total_calls >= 0,
              }}
              hint={`vs previous ${days}d`}
            />
            <StatCard
              label="Resolution rate"
              value={percent(summary.data.resolution_rate)}
              delta={{
                value: signedPercent(
                  summary.data.deltas.resolution_rate * 100,
                ),
                positive: summary.data.deltas.resolution_rate >= 0,
              }}
              hint={`${number(summary.data.resolved_calls)} resolved`}
            />
            <StatCard
              label="Avg call length"
              value={duration(Math.round(summary.data.avg_duration_sec))}
              hint={`${number(summary.data.total_billable_minutes)} billable min`}
            />
            <StatCard
              label="Call spend"
              value={rupees(summary.data.total_cost_paise, { compact: true })}
              hint={`${number(summary.data.active_agents)} active agent(s)`}
            />
          </div>

          <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <StatCard
              label="Answer rate"
              value={percent(summary.data.answer_rate)}
              hint={`${number(summary.data.answered_calls)} answered`}
            />
            <StatCard
              label="Inbound / outbound"
              value={`${number(summary.data.inbound_calls)} / ${number(
                summary.data.outbound_calls,
              )}`}
            />
            <StatCard
              label="Handed off"
              value={number(summary.data.handed_off_calls)}
              hint="escalated to WhatsApp or a human"
            />
            <StatCard
              label="Sentiment"
              value={`${number(summary.data.positive_sentiment)} / ${number(
                summary.data.negative_sentiment,
              )}`}
              hint="positive / negative"
            />
          </div>
        </>
      ) : null}

      <div className="mt-6 grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader
            title="Daily call volume"
            subtitle="From the nightly analytics rollup"
          />
          {analytics.error ? (
            <div className="p-5">
              <ErrorNotice message={analytics.error} onRetry={analytics.reload} />
            </div>
          ) : analytics.loading && !analytics.data ? (
            <div className="p-5">
              <Spinner />
            </div>
          ) : (
            <BarChart
              label="Daily call volume"
              data={(analytics.data?.series ?? []).map((point) => ({
                date: point.date,
                value: point.total_calls,
              }))}
              formatValue={(value) => `${number(value)} calls`}
            />
          )}
        </Card>

        <Card>
          <CardHeader title="Languages" subtitle={`Last ${days} days`} />
          {analytics.data ? (
            <DistributionBars
              items={Object.entries(analytics.data.languages)
                .map(([code, count]) => ({
                  label: languageName(code),
                  value: count,
                }))
                .sort((a, b) => b.value - a.value)}
              formatValue={(value) => `${number(value)} calls`}
            />
          ) : (
            <div className="p-5">
              <Spinner />
            </div>
          )}
        </Card>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="Top intents" subtitle={`Last ${days} days`} />
          {analytics.data ? (
            <DistributionBars
              items={analytics.data.intents.slice(0, 8).map((intent) => ({
                label: titleCase(intent.intent),
                value: intent.count,
              }))}
              formatValue={(value) => `${number(value)} calls`}
            />
          ) : (
            <div className="p-5">
              <Spinner />
            </div>
          )}
        </Card>

        <Card>
          <CardHeader title="Agent performance" subtitle={`Last ${days} days`} />
          {leaderboard.error ? (
            <div className="p-5">
              <ErrorNotice
                message={leaderboard.error}
                onRetry={leaderboard.reload}
              />
            </div>
          ) : leaderboard.data && leaderboard.data.length > 0 ? (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-ink-100 text-left text-xs uppercase tracking-wide text-ink-500">
                  <th className="px-5 py-2 font-medium">Agent</th>
                  <th className="px-3 py-2 text-right font-medium">Calls</th>
                  <th className="px-3 py-2 text-right font-medium">Resolved</th>
                  <th className="px-5 py-2 text-right font-medium">Avg</th>
                </tr>
              </thead>
              <tbody>
                {leaderboard.data.map((row) => (
                  <tr
                    key={row.agent_id ?? row.agent_name}
                    className="border-b border-ink-50 last:border-0"
                  >
                    <td className="px-5 py-2.5 text-ink-900">
                      {row.agent_name}
                    </td>
                    <td className="tnum px-3 py-2.5 text-right text-ink-600">
                      {number(row.total_calls)}
                    </td>
                    <td className="tnum px-3 py-2.5 text-right text-ink-600">
                      {percent(row.resolution_rate)}
                    </td>
                    <td className="tnum px-5 py-2.5 text-right text-ink-600">
                      {duration(Math.round(row.avg_duration_sec))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="px-5 py-10 text-center text-sm text-ink-500">
              No agent traffic in this period.
            </p>
          )}
        </Card>
      </div>
    </div>
  );
}
