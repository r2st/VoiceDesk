"use client";

import { Badge, type BadgeTone } from "@/components/ui";
import { titleCase } from "@/lib/format";
import type { AppointmentSource, AppointmentStatus } from "@/lib/types";

const STATUS_TONE: Record<AppointmentStatus, BadgeTone> = {
  scheduled: "neutral",
  confirmed: "success",
  completed: "success",
  cancelled: "neutral",
  no_show: "danger",
  rescheduled: "info",
};

const STATUS_LABEL: Partial<Record<AppointmentStatus, string>> = {
  no_show: "No-show",
};

const SOURCE_LABEL: Record<AppointmentSource, string> = {
  voice_call: "Booked by agent",
  dashboard: "Booked by staff",
  whatsapp: "WhatsApp",
  api: "API",
};

export function AppointmentStatusBadge({
  status,
}: {
  status: AppointmentStatus;
}) {
  return (
    <Badge tone={STATUS_TONE[status] ?? "neutral"}>
      {STATUS_LABEL[status] ?? titleCase(status)}
    </Badge>
  );
}

/**
 * Who made the booking.
 *
 * Worth its own column: a booking the voice agent placed unattended is the
 * one a receptionist most wants to sanity-check before the customer arrives.
 */
export function SourceBadge({ source }: { source: AppointmentSource }) {
  return (
    <span className="text-xs text-ink-500">
      {SOURCE_LABEL[source] ?? titleCase(source)}
    </span>
  );
}
