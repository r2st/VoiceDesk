"use client";

import { useState } from "react";

import { api } from "@/lib/api";
import { useApi } from "@/lib/use-api";
import {
  dayLabel,
  duration,
  istDayWindow,
  istToday,
  number,
  phone,
  shiftDay,
  timeOfDay,
} from "@/lib/format";
import {
  AppointmentStatusBadge,
  SourceBadge,
} from "@/components/appointment-badges";
import {
  Card,
  EmptyState,
  ErrorNotice,
  Spinner,
  StatCard,
} from "@/components/ui";
import type { AppointmentStatus } from "@/lib/types";

import { AppointmentDrawer } from "./appointment-drawer";

/** A day's worth of bookings comfortably fits one page. */
const DAY_LIMIT = 200;

/** Statuses that still occupy a slot, and so count as the day's workload. */
const ACTIVE: AppointmentStatus[] = ["scheduled", "confirmed"];

export default function AppointmentsPage() {
  const [day, setDay] = useState(istToday);
  const [openId, setOpenId] = useState<string | null>(null);

  const dayWindow = istDayWindow(day);
  const bookings = useApi(
    () =>
      api.appointments.list({
        limit: DAY_LIMIT,
        starts_after: dayWindow.from,
        starts_before: dayWindow.to,
      }),
    [day],
  );
  const availability = useApi(() => api.appointments.availability(day), [day]);

  function refreshAll() {
    bookings.reload();
    availability.reload();
  }

  const items = bookings.data?.items ?? [];
  // The API sorts by creation; a day view is only useful in clock order.
  const ordered = [...items].sort((a, b) =>
    a.scheduled_at.localeCompare(b.scheduled_at),
  );

  const active = ordered.filter((a) => ACTIVE.includes(a.status));
  const bookedMinutes = active.reduce((sum, a) => sum + a.duration_minutes, 0);
  const freeSlots =
    availability.data?.slots.filter((s) => s.remaining_capacity > 0).length ?? 0;
  const isToday = day === istToday();

  return (
    <div className="mx-auto max-w-6xl">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-ink-900">Appointments</h1>
          <p className="text-sm text-ink-500">
            {dayLabel(day)}
            {isToday ? " · today" : ""}
          </p>
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setDay(shiftDay(day, -1))}
            aria-label="Previous day"
            className="rounded-lg border border-ink-200 bg-white px-2.5 py-1.5 text-sm font-medium text-ink-700 hover:bg-ink-50"
          >
            ←
          </button>
          <input
            type="date"
            value={day}
            onChange={(e) => setDay(e.target.value || istToday())}
            aria-label="Pick a day"
            className="rounded-lg border border-ink-200 bg-white px-3 py-1.5 text-sm text-ink-700"
          />
          <button
            type="button"
            onClick={() => setDay(shiftDay(day, 1))}
            aria-label="Next day"
            className="rounded-lg border border-ink-200 bg-white px-2.5 py-1.5 text-sm font-medium text-ink-700 hover:bg-ink-50"
          >
            →
          </button>
          <button
            type="button"
            disabled={isToday}
            onClick={() => setDay(istToday())}
            className="rounded-lg border border-ink-200 bg-white px-3 py-1.5 text-sm font-medium text-ink-700 hover:bg-ink-50 disabled:opacity-40"
          >
            Today
          </button>
        </div>
      </div>

      <div className="mt-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="Booked" value={number(active.length)} />
        <StatCard
          label="Time committed"
          value={duration(bookedMinutes * 60)}
          hint="across confirmed and scheduled"
        />
        <StatCard
          label="Slots still free"
          value={availability.data?.is_open ? number(freeSlots) : "Closed"}
          hint={availability.data?.is_open ? undefined : "not a working day"}
        />
        <StatCard
          label="Needs attention"
          value={number(ordered.filter((a) => a.status === "scheduled").length)}
          hint="not yet confirmed"
        />
      </div>

      <Card className="mt-5 overflow-hidden">
        {bookings.error ? (
          <div className="p-5">
            <ErrorNotice message={bookings.error} onRetry={bookings.reload} />
          </div>
        ) : bookings.loading && !bookings.data ? (
          <div className="p-5">
            <Spinner label="Loading the day" />
          </div>
        ) : ordered.length === 0 ? (
          <EmptyState
            title="Nothing booked for this day"
            description={
              availability.data?.is_open === false
                ? "This is not a working day on your calendar, so the agent will not offer it either."
                : "Bookings your agents take on calls appear here alongside anything staff enter."
            }
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[820px] text-sm">
              <thead>
                <tr className="border-b border-ink-100 text-left text-xs uppercase tracking-wide text-ink-500">
                  <th className="px-5 py-2.5 font-medium">Time</th>
                  <th className="px-3 py-2.5 font-medium">Customer</th>
                  <th className="px-3 py-2.5 font-medium">Service</th>
                  <th className="px-3 py-2.5 font-medium">Status</th>
                  <th className="px-3 py-2.5 font-medium">Booked by</th>
                  <th className="px-5 py-2.5 text-right font-medium">Length</th>
                </tr>
              </thead>
              <tbody>
                {ordered.map((booking) => (
                  <tr
                    key={booking.id}
                    className={`border-b border-ink-50 transition last:border-0 hover:bg-ink-50 ${
                      booking.status === "cancelled" ? "opacity-60" : ""
                    }`}
                  >
                    <td className="tnum px-5 py-3 whitespace-nowrap">
                      <button
                        type="button"
                        onClick={() => setOpenId(booking.id)}
                        className="font-medium text-brand-700 hover:underline"
                      >
                        {timeOfDay(booking.scheduled_at)}
                      </button>
                    </td>
                    <td className="px-3 py-3">
                      <p className="text-ink-800">{booking.customer_name}</p>
                      <p className="tnum text-xs text-ink-500">
                        {phone(booking.customer_phone)}
                      </p>
                    </td>
                    <td className="px-3 py-3 text-ink-600">
                      {booking.service ?? <span className="text-ink-400">—</span>}
                    </td>
                    <td className="px-3 py-3">
                      <AppointmentStatusBadge status={booking.status} />
                    </td>
                    <td className="px-3 py-3">
                      <SourceBadge source={booking.source} />
                    </td>
                    <td className="tnum px-5 py-3 text-right text-ink-600">
                      {duration(booking.duration_minutes * 60)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {openId ? (
        <AppointmentDrawer
          appointmentId={openId}
          onClose={() => setOpenId(null)}
          onChanged={refreshAll}
        />
      ) : null}
    </div>
  );
}
