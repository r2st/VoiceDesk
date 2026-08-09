"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { ApiError, api } from "@/lib/api";
import { useApi } from "@/lib/use-api";
import {
  dateTime,
  dayLabel,
  duration,
  istDay,
  istToday,
  languageName,
  phone,
  shiftDay,
  timeOfDay,
} from "@/lib/format";
import {
  AppointmentStatusBadge,
  SourceBadge,
} from "@/components/appointment-badges";
import { Badge, ErrorNotice, Spinner } from "@/components/ui";
import type { AppointmentStatus } from "@/lib/types";

/**
 * Mirrors ``appointment_service.ALLOWED_TRANSITIONS``.
 *
 * Cancellation is deliberately absent: it carries a reason and has its own
 * endpoint, so it gets its own control rather than sitting in this row.
 * Keeping the map in sync with the service is what stops the drawer offering
 * a button whose only possible outcome is a 422.
 */
const NEXT_STATUSES: Record<AppointmentStatus, AppointmentStatus[]> = {
  scheduled: ["confirmed", "completed", "no_show"],
  confirmed: ["completed", "no_show"],
  completed: [],
  cancelled: [],
  no_show: ["completed"],
  rescheduled: [],
};

const ACTION_LABEL: Record<AppointmentStatus, string> = {
  scheduled: "Reopen",
  confirmed: "Confirm",
  completed: "Mark completed",
  cancelled: "Cancel",
  no_show: "Mark no-show",
  rescheduled: "Rescheduled",
};

/** How far ahead the reschedule picker will let staff step, in days. */
const RESCHEDULE_HORIZON = 60;

export function AppointmentDrawer({
  appointmentId,
  onClose,
  onChanged,
}: {
  appointmentId: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const booking = useApi(
    () => api.appointments.get(appointmentId),
    [appointmentId],
  );
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [cancelReason, setCancelReason] = useState("");
  const [moveTo, setMoveTo] = useState<string | null>(null);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function run(key: string, action: () => Promise<unknown>) {
    setBusy(key);
    setActionError(null);
    try {
      await action();
      booking.reload();
      onChanged();
      return true;
    } catch (err: unknown) {
      setActionError(
        err instanceof ApiError
          ? err.message
          : "Could not update this appointment.",
      );
      return false;
    } finally {
      setBusy(null);
    }
  }

  const data = booking.data;
  const canMove =
    data !== null &&
    data.status !== "completed" &&
    data.status !== "cancelled";

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <div
        aria-hidden="true"
        onClick={onClose}
        className="absolute inset-0 bg-ink-900/20"
      />

      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Appointment detail"
        className="relative flex h-full w-full max-w-lg flex-col overflow-y-auto border-l border-ink-200 bg-white shadow-xl"
      >
        <header className="sticky top-0 z-10 flex items-start justify-between gap-4 border-b border-ink-100 bg-white px-5 py-4">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold text-ink-900">
              {data?.customer_name ?? "Appointment"}
            </h2>
            {data ? (
              <p className="tnum mt-0.5 text-xs text-ink-500">
                {phone(data.customer_phone)}
              </p>
            ) : null}
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-ink-200 px-2.5 py-1 text-xs font-medium text-ink-700 hover:bg-ink-50"
          >
            Close
          </button>
        </header>

        {booking.error ? (
          <div className="p-5">
            <ErrorNotice message={booking.error} onRetry={booking.reload} />
          </div>
        ) : !data ? (
          <div className="p-5">
            <Spinner label="Loading appointment" />
          </div>
        ) : (
          <div className="flex-1 px-5 py-4">
            <div className="flex flex-wrap items-center gap-2">
              <AppointmentStatusBadge status={data.status} />
              <SourceBadge source={data.source} />
            </div>

            <div className="mt-4 rounded-lg border border-ink-100 px-4 py-3">
              <p className="text-sm font-medium text-ink-900">
                {dayLabel(istDay(data.scheduled_at))}
              </p>
              <p className="tnum mt-0.5 text-sm text-ink-600">
                {timeOfDay(data.scheduled_at)} ·{" "}
                {duration(data.duration_minutes * 60)}
              </p>
            </div>

            <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
              <Field label="Service" value={data.service ?? "—"} />
              <Field label="Language" value={languageName(data.language)} />
              <Field label="Booked" value={dateTime(data.created_at)} />
              <Field
                label="Email"
                value={data.customer_email ?? "—"}
              />
            </dl>

            {data.confirmed_at ? (
              <p className="mt-3 text-xs text-ink-500">
                Confirmed {dateTime(data.confirmed_at)}.
              </p>
            ) : null}

            {data.cancelled_at ? (
              <p className="mt-3 rounded-lg border border-ink-200 bg-ink-50 px-3 py-2 text-xs text-ink-700">
                Cancelled {dateTime(data.cancelled_at)}
                {data.cancellation_reason
                  ? ` — ${data.cancellation_reason}`
                  : "."}
              </p>
            ) : null}

            {/* Reminder state is the first thing a receptionist checks before
                chasing a customer, so it is stated rather than left implied. */}
            <p className="mt-3 text-xs text-ink-500">
              {data.reminder_sent_at
                ? `Reminder sent ${dateTime(data.reminder_sent_at)}.`
                : "No reminder sent yet."}
            </p>

            {data.notes ? (
              <>
                <h3 className="mt-6 text-xs font-semibold uppercase tracking-wide text-ink-500">
                  Notes
                </h3>
                <p className="mt-2 whitespace-pre-wrap text-sm text-ink-700">
                  {data.notes}
                </p>
              </>
            ) : null}

            {data.call_id ? (
              <Link
                href={`/calls/${data.call_id}`}
                className="mt-6 inline-block text-sm font-medium text-brand-700 hover:underline"
              >
                Open the call that booked this →
              </Link>
            ) : null}

            {canMove ? (
              <div className="mt-6">
                {moveTo === null ? (
                  <button
                    type="button"
                    onClick={() =>
                      setMoveTo(maxDay(istDay(data.scheduled_at), istToday()))
                    }
                    className="rounded-lg border border-ink-200 px-3 py-1.5 text-sm font-medium text-ink-700 hover:bg-ink-50"
                  >
                    Move to another time
                  </button>
                ) : (
                  <ReschedulePicker
                    day={moveTo}
                    durationMinutes={data.duration_minutes}
                    busy={busy !== null}
                    onDayChange={setMoveTo}
                    onCancel={() => setMoveTo(null)}
                    onPick={async (start) => {
                      const ok = await run("reschedule", () =>
                        api.appointments.reschedule(data.id, start),
                      );
                      if (ok) setMoveTo(null);
                    }}
                  />
                )}
              </div>
            ) : null}
          </div>
        )}

        {data ? (
          <footer className="sticky bottom-0 border-t border-ink-100 bg-white px-5 py-3">
            {actionError ? (
              <p className="mb-2 text-xs text-rose-700">{actionError}</p>
            ) : null}

            {cancelling ? (
              <div>
                <label
                  htmlFor="cancel-reason"
                  className="text-xs font-medium text-ink-700"
                >
                  Why is this being cancelled?
                </label>
                <input
                  id="cancel-reason"
                  value={cancelReason}
                  onChange={(e) => setCancelReason(e.target.value)}
                  maxLength={500}
                  placeholder="Customer called to cancel"
                  className="mt-1 w-full rounded-lg border border-ink-200 px-3 py-1.5 text-sm text-ink-800"
                />
                <div className="mt-2 flex gap-2">
                  <button
                    type="button"
                    disabled={busy !== null}
                    onClick={() =>
                      void run("cancel", () =>
                        api.appointments.cancel(data.id, cancelReason.trim()),
                      ).then((ok) => {
                        if (ok) setCancelling(false);
                      })
                    }
                    className="rounded-lg bg-rose-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-rose-700 disabled:opacity-40"
                  >
                    {busy === "cancel" ? "Cancelling…" : "Cancel booking"}
                  </button>
                  <button
                    type="button"
                    disabled={busy !== null}
                    onClick={() => setCancelling(false)}
                    className="rounded-lg border border-ink-200 px-3 py-1.5 text-sm font-medium text-ink-700 hover:bg-ink-50"
                  >
                    Keep it
                  </button>
                </div>
              </div>
            ) : (
              <div className="flex flex-wrap gap-2">
                {NEXT_STATUSES[data.status].map((status) => (
                  <button
                    key={status}
                    type="button"
                    disabled={busy !== null}
                    onClick={() =>
                      void run(status, () =>
                        api.appointments.setStatus(data.id, status),
                      )
                    }
                    className="rounded-lg border border-ink-200 px-3 py-1.5 text-sm font-medium text-ink-700 transition hover:bg-ink-50 disabled:opacity-40"
                  >
                    {busy === status ? "Saving…" : ACTION_LABEL[status]}
                  </button>
                ))}
                {canMove ? (
                  <button
                    type="button"
                    disabled={busy !== null}
                    onClick={() => setCancelling(true)}
                    className="rounded-lg border border-rose-200 px-3 py-1.5 text-sm font-medium text-rose-700 transition hover:bg-rose-50 disabled:opacity-40"
                  >
                    Cancel…
                  </button>
                ) : null}
                {NEXT_STATUSES[data.status].length === 0 && !canMove ? (
                  <p className="text-xs text-ink-500">
                    This appointment is closed; nothing further to do.
                  </p>
                ) : null}
              </div>
            )}
          </footer>
        ) : null}
      </aside>
    </div>
  );
}

/**
 * Slot grid for a chosen day.
 *
 * Staff pick from real availability rather than typing a time: the API rejects
 * a full or out-of-hours slot anyway, and a picker that can only offer valid
 * choices turns that rejection into something the user never sees.
 */
function ReschedulePicker({
  day,
  durationMinutes,
  busy,
  onDayChange,
  onCancel,
  onPick,
}: {
  day: string;
  durationMinutes: number;
  busy: boolean;
  onDayChange: (day: string) => void;
  onCancel: () => void;
  onPick: (start: string) => void | Promise<void>;
}) {
  const availability = useApi(
    () => api.appointments.availability(day, durationMinutes),
    [day, durationMinutes],
  );

  const today = istToday();
  const horizon = shiftDay(today, RESCHEDULE_HORIZON);
  const open = availability.data?.slots.filter((s) => s.remaining_capacity > 0);

  return (
    <div className="rounded-lg border border-ink-200 px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-500">
          Move to
        </h3>
        <button
          type="button"
          onClick={onCancel}
          className="text-xs font-medium text-ink-500 hover:text-ink-800"
        >
          Never mind
        </button>
      </div>

      <div className="mt-2 flex items-center gap-2">
        <button
          type="button"
          aria-label="Previous day"
          disabled={day <= today}
          onClick={() => onDayChange(shiftDay(day, -1))}
          className="rounded-lg border border-ink-200 px-2 py-1 text-sm text-ink-700 hover:bg-ink-50 disabled:opacity-40"
        >
          ←
        </button>
        <input
          type="date"
          value={day}
          min={today}
          max={horizon}
          aria-label="Reschedule to day"
          onChange={(e) => onDayChange(e.target.value || today)}
          className="rounded-lg border border-ink-200 px-3 py-1 text-sm text-ink-700"
        />
        <button
          type="button"
          aria-label="Next day"
          disabled={day >= horizon}
          onClick={() => onDayChange(shiftDay(day, 1))}
          className="rounded-lg border border-ink-200 px-2 py-1 text-sm text-ink-700 hover:bg-ink-50 disabled:opacity-40"
        >
          →
        </button>
      </div>

      <div className="mt-3">
        {availability.error ? (
          <ErrorNotice
            message={availability.error}
            onRetry={availability.reload}
          />
        ) : availability.loading && !availability.data ? (
          <Spinner label="Checking the calendar" />
        ) : !availability.data?.is_open ? (
          <p className="text-xs text-ink-500">
            {dayLabel(day)} is not a working day on your calendar.
          </p>
        ) : open && open.length > 0 ? (
          <>
            <div className="flex flex-wrap gap-1.5">
              {open.map((slot) => (
                <button
                  key={slot.start}
                  type="button"
                  disabled={busy}
                  onClick={() => void onPick(slot.start)}
                  className="tnum rounded-lg border border-ink-200 px-2.5 py-1 text-xs font-medium text-ink-700 transition hover:border-brand-300 hover:bg-brand-50 hover:text-brand-700 disabled:opacity-40"
                >
                  {timeOfDay(slot.start)}
                </button>
              ))}
            </div>
            {/* Capacity is per slot, so a "free" slot may still be the last
                one. Saying how many are left stops a surprising conflict. */}
            <p className="mt-2 text-xs text-ink-500">
              {open.length} opening{open.length === 1 ? "" : "s"} of{" "}
              {duration(durationMinutes * 60)} on {dayLabel(day)}.
            </p>
          </>
        ) : (
          <div className="flex items-center gap-2">
            <Badge tone="warning">Fully booked</Badge>
            <span className="text-xs text-ink-500">
              Nothing free on {dayLabel(day)}.
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

/** The later of two `YYYY-MM-DD` days — ISO dates compare correctly as text. */
function maxDay(a: string, b: string): string {
  return a >= b ? a : b;
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-ink-500">{label}</dt>
      <dd className="truncate text-ink-800">{value}</dd>
    </div>
  );
}
