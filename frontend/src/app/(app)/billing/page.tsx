"use client";

import { api } from "@/lib/api";
import { useApi } from "@/lib/use-api";
import { monthLabel, number, rupees, titleCase } from "@/lib/format";
import {
  Badge,
  Card,
  CardHeader,
  ErrorNotice,
  Spinner,
  StatCard,
} from "@/components/ui";

export default function BillingPage() {
  const current = useApi(() => api.billing.usage(), []);
  const history = useApi(() => api.billing.history(), []);

  return (
    <div className="mx-auto max-w-5xl">
      <h1 className="text-lg font-semibold text-ink-900">Billing</h1>
      <p className="text-sm text-ink-500">
        Usage this cycle and your invoice history. All amounts include 18% GST.
      </p>

      {current.error ? (
        <div className="mt-5">
          <ErrorNotice message={current.error} onRetry={current.reload} />
        </div>
      ) : current.loading && !current.data ? (
        <div className="mt-6">
          <Spinner label="Loading usage" />
        </div>
      ) : current.data ? (
        <>
          <div className="mt-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <StatCard
              label="Plan"
              value={current.data.plan.name}
              hint={`${number(current.data.plan.included_minutes)} min included`}
            />
            <StatCard
              label="Minutes used"
              value={number(current.data.quota.minutes_used)}
              hint={`${number(current.data.quota.remaining_minutes)} remaining`}
            />
            <StatCard
              label="This cycle"
              value={rupees(current.data.usage.total_paise)}
              hint={monthLabel(current.data.usage.month)}
            />
            <StatCard
              label="Projected total"
              value={rupees(current.data.projected_total_paise)}
              hint="straight-line to month end"
            />
          </div>

          <Card className="mt-4">
            <CardHeader
              title={`Usage for ${monthLabel(current.data.usage.month)}`}
              subtitle={`${number(current.data.usage.calls_count)} billable call(s)`}
              action={
                current.data.quota.in_overage ? (
                  <Badge tone="warning">In overage</Badge>
                ) : (
                  <Badge tone="success">Within plan</Badge>
                )
              }
            />

            <div className="px-5 pt-4">
              <div className="flex justify-between text-xs text-ink-500">
                <span>
                  {number(current.data.quota.minutes_used)} of{" "}
                  {number(current.data.quota.included_minutes)} included minutes
                </span>
                <span className="tnum">
                  {current.data.quota.utilization_pct.toFixed(1)}%
                </span>
              </div>
              <div className="mt-1.5 h-2 overflow-hidden rounded-full bg-ink-100">
                <div
                  className={`h-full rounded-full ${
                    current.data.quota.in_overage
                      ? "bg-amber-500"
                      : "bg-brand-500"
                  }`}
                  style={{
                    width: `${Math.min(100, current.data.quota.utilization_pct)}%`,
                  }}
                />
              </div>
            </div>

            <dl className="grid gap-x-8 gap-y-2 px-5 py-4 text-sm sm:grid-cols-2">
              <LineItem
                label="Plan fee"
                value={rupees(current.data.usage.base_fee_paise)}
              />
              <LineItem
                label={`Overage (${number(current.data.usage.overage_minutes)} min)`}
                value={rupees(current.data.usage.overage_paise)}
              />
              <LineItem
                label="Number rent"
                value={rupees(current.data.usage.number_rent_paise)}
              />
              <LineItem
                label="GST (18%)"
                value={rupees(current.data.usage.tax_paise)}
              />
              <LineItem
                label="Total"
                value={rupees(current.data.usage.total_paise)}
                strong
              />
            </dl>
          </Card>
        </>
      ) : null}

      <Card className="mt-4 overflow-hidden">
        <CardHeader title="Invoice history" subtitle="Most recent cycles first" />
        {history.error ? (
          <div className="p-5">
            <ErrorNotice message={history.error} onRetry={history.reload} />
          </div>
        ) : history.loading && !history.data ? (
          <div className="p-5">
            <Spinner />
          </div>
        ) : history.data && history.data.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm">
              <thead>
                <tr className="border-b border-ink-100 text-left text-xs uppercase tracking-wide text-ink-500">
                  <th className="px-5 py-2.5 font-medium">Period</th>
                  <th className="px-3 py-2.5 font-medium">Plan</th>
                  <th className="px-3 py-2.5 text-right font-medium">Minutes</th>
                  <th className="px-3 py-2.5 text-right font-medium">Calls</th>
                  <th className="px-3 py-2.5 font-medium">Invoice</th>
                  <th className="px-5 py-2.5 text-right font-medium">Total</th>
                </tr>
              </thead>
              <tbody>
                {history.data.map((row) => (
                  <tr key={row.id} className="border-b border-ink-50 last:border-0">
                    <td className="px-5 py-3 font-medium text-ink-900">
                      {monthLabel(row.month)}
                    </td>
                    <td className="px-3 py-3 text-ink-600">
                      {titleCase(row.plan_id)}
                    </td>
                    <td className="tnum px-3 py-3 text-right text-ink-600">
                      {number(row.minutes_used)}
                    </td>
                    <td className="tnum px-3 py-3 text-right text-ink-600">
                      {number(row.calls_count)}
                    </td>
                    <td className="px-3 py-3 text-xs text-ink-500">
                      {row.invoice_number ?? (
                        <Badge tone="neutral">Open</Badge>
                      )}
                    </td>
                    <td className="tnum px-5 py-3 text-right font-medium text-ink-900">
                      {rupees(row.total_paise)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="px-5 py-10 text-center text-sm text-ink-500">
            No completed billing cycles yet.
          </p>
        )}
      </Card>
    </div>
  );
}

function LineItem({
  label,
  value,
  strong = false,
}: {
  label: string;
  value: string;
  strong?: boolean;
}) {
  return (
    <div
      className={`flex justify-between gap-4 border-b border-ink-50 pb-2 ${
        strong ? "font-semibold text-ink-900" : ""
      }`}
    >
      <dt className={strong ? "" : "text-ink-500"}>{label}</dt>
      <dd className="tnum">{value}</dd>
    </div>
  );
}
