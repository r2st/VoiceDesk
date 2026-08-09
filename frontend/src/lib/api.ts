/**
 * Typed client for the VoiceDesk API.
 *
 * Tokens live in localStorage and go out as a bearer header, matching how the
 * backend authenticates. A 401 triggers exactly one refresh-and-retry: the
 * access token is short-lived (15 minutes) so an expiring token mid-session is
 * routine, but a second failure means the refresh token is spent too and the
 * only correct move is to send the user back to the sign-in screen.
 */

import type {
  AgentLeaderboardEntry,
  Appointment,
  AppointmentStatus,
  Availability,
  Business,
  Call,
  CallAnalytics,
  CallDetail,
  CallSnapshot,
  ConversationTurn,
  CurrentUsage,
  DashboardSummary,
  Lead,
  LeadDetail,
  LeadStatus,
  LeadTier,
  LiveCall,
  Page,
  PhoneNumber,
  PipelineSummary,
  Plan,
  QualificationConfig,
  QuotaStatus,
  ScheduleConfig,
  Slot,
  Takeover,
  TokenPair,
  Usage,
  User,
  VoiceAgent,
} from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:3010/api/v1";

const ACCESS_KEY = "voicedesk.access_token";
const REFRESH_KEY = "voicedesk.refresh_token";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }

  /** The account cannot transact — suspended, cancelled or over quota. */
  get isPaymentRequired(): boolean {
    return this.status === 402;
  }
}

// --------------------------------------------------------------------------- //
// Token storage
// --------------------------------------------------------------------------- //
export const tokens = {
  access(): string | null {
    if (typeof window === "undefined") return null;
    return window.localStorage.getItem(ACCESS_KEY);
  },
  refresh(): string | null {
    if (typeof window === "undefined") return null;
    return window.localStorage.getItem(REFRESH_KEY);
  },
  save(pair: TokenPair): void {
    window.localStorage.setItem(ACCESS_KEY, pair.access_token);
    window.localStorage.setItem(REFRESH_KEY, pair.refresh_token);
  },
  clear(): void {
    if (typeof window === "undefined") return;
    window.localStorage.removeItem(ACCESS_KEY);
    window.localStorage.removeItem(REFRESH_KEY);
  },
};

// --------------------------------------------------------------------------- //
// Core request pipeline
// --------------------------------------------------------------------------- //
interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null>;
  /** Internal: prevents a refresh loop when the refresh call itself 401s. */
  retryOnUnauthorized?: boolean;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const url = new URL(`${API_BASE}${path}`);
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

async function toApiError(response: Response): Promise<ApiError> {
  let code = "error";
  let message = response.statusText || "Request failed.";
  let details: Record<string, unknown> = {};
  try {
    const body = await response.json();
    if (body?.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
      details = body.error.details ?? {};
    }
  } catch {
    // A non-JSON body (a proxy error page, say) leaves the status text as the
    // best available message.
  }
  return new ApiError(response.status, code, message, details);
}

/** Refreshes the token pair in place. Returns false if the session is over. */
async function refreshSession(): Promise<boolean> {
  const refreshToken = tokens.refresh();
  if (!refreshToken) return false;

  const response = await fetch(buildUrl("/auth/refresh"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
  });
  if (!response.ok) {
    tokens.clear();
    return false;
  }
  tokens.save((await response.json()) as TokenPair);
  return true;
}

export async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { method = "GET", body, query, retryOnUnauthorized = true } = options;

  const headers: Record<string, string> = {};
  const access = tokens.access();
  if (access) headers.Authorization = `Bearer ${access}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const response = await fetch(buildUrl(path, query), {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (response.status === 401 && retryOnUnauthorized && tokens.refresh()) {
    if (await refreshSession()) {
      return request<T>(path, { ...options, retryOnUnauthorized: false });
    }
  }

  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

// --------------------------------------------------------------------------- //
// Endpoints
// --------------------------------------------------------------------------- //
export const api = {
  auth: {
    login: (email: string, password: string, businessSlug?: string) =>
      request<TokenPair>("/auth/login", {
        method: "POST",
        body: { email, password, business_slug: businessSlug || null },
        retryOnUnauthorized: false,
      }),
    me: () => request<User>("/auth/me"),
    business: () => request<Business>("/auth/business"),
    logout: (refreshToken: string) =>
      request<{ message: string }>("/auth/logout", {
        method: "POST",
        body: { refresh_token: refreshToken },
        retryOnUnauthorized: false,
      }),
  },

  analytics: {
    dashboard: (days = 30) =>
      request<DashboardSummary>("/analytics/dashboard", { query: { days } }),
    calls: (params: { date_from?: string; date_to?: string; agent_id?: string }) =>
      request<CallAnalytics>("/analytics/calls", { query: params }),
    agents: (days = 30) =>
      request<AgentLeaderboardEntry[]>("/analytics/agents", { query: { days } }),
  },

  agents: {
    list: (limit = 50, offset = 0) =>
      request<Page<VoiceAgent>>("/agents", { query: { limit, offset } }),
    get: (id: string) => request<VoiceAgent>(`/agents/${id}`),
    update: (id: string, patch: Partial<VoiceAgent>) =>
      request<VoiceAgent>(`/agents/${id}`, { method: "PATCH", body: patch }),
  },

  calls: {
    list: (params: {
      limit?: number;
      offset?: number;
      status?: string;
      direction?: string;
      agent_id?: string;
    }) => request<Page<Call>>("/calls", { query: params }),
    get: (id: string) => request<CallDetail>(`/calls/${id}`),
    transcript: (id: string) =>
      request<ConversationTurn[]>(`/calls/${id}/transcript`),
  },

  monitor: {
    live: () => request<LiveCall[]>("/monitor/live"),
    snapshot: (callId: string) =>
      request<CallSnapshot>(`/monitor/calls/${callId}`),
    takeOver: (callId: string, reason?: string) =>
      request<Takeover>(`/monitor/calls/${callId}/takeover`, {
        method: "POST",
        body: { reason: reason || null },
      }),
    release: (callId: string, returnToAi = true) =>
      request<Takeover>(`/monitor/calls/${callId}/release`, {
        method: "POST",
        body: { return_to_ai: returnToAi },
      }),
    say: (callId: string, text: string) =>
      request<ConversationTurn>(`/monitor/calls/${callId}/say`, {
        method: "POST",
        body: { text },
      }),
  },

  appointments: {
    list: (params: {
      limit?: number;
      offset?: number;
      status?: AppointmentStatus | "";
      /** UTC instants. The caller converts the tenant's day into a window. */
      starts_after?: string;
      starts_before?: string;
      search?: string;
    }) => request<Page<Appointment>>("/appointments", { query: params }),
    get: (id: string) => request<Appointment>(`/appointments/${id}`),
    /** `date` is a plain `YYYY-MM-DD` in the tenant's timezone. */
    availability: (date: string, durationMinutes?: number) =>
      request<Availability>("/appointments/availability", {
        query: { date, duration_minutes: durationMinutes },
      }),
    nextAvailable: (limit = 3) =>
      request<Slot[]>("/appointments/next-available", { query: { limit } }),
    book: (body: {
      customer_name: string;
      customer_phone: string;
      scheduled_at: string;
      duration_minutes?: number;
      service?: string | null;
      notes?: string | null;
      override_hours?: boolean;
    }) => request<Appointment>("/appointments", { method: "POST", body }),
    reschedule: (id: string, scheduledAt: string, reason?: string) =>
      request<Appointment>(`/appointments/${id}/reschedule`, {
        method: "POST",
        body: { scheduled_at: scheduledAt, reason: reason || null },
      }),
    cancel: (id: string, reason?: string) =>
      request<Appointment>(`/appointments/${id}/cancel`, {
        method: "POST",
        body: { reason: reason || null },
      }),
    setStatus: (id: string, status: AppointmentStatus) =>
      request<Appointment>(`/appointments/${id}/status`, {
        method: "POST",
        body: { status },
      }),
    schedule: () => request<ScheduleConfig>("/appointments/schedule"),
  },

  leads: {
    list: (params: {
      limit?: number;
      offset?: number;
      status?: LeadStatus | "";
      tier?: LeadTier | "";
      min_score?: number;
      contact_phone?: string;
    }) => request<Page<Lead>>("/leads", { query: params }),
    get: (id: string) => request<LeadDetail>(`/leads/${id}`),
    summary: () => request<PipelineSummary>("/leads/summary"),
    /** Staff may only move a lead along; the score decides the rest. */
    setStatus: (id: string, status: LeadStatus) =>
      request<LeadDetail>(`/leads/${id}`, { method: "PATCH", body: { status } }),
    rescore: (id: string) =>
      request<LeadDetail>(`/leads/${id}/rescore`, { method: "POST" }),
    config: () => request<QualificationConfig>("/leads/config"),
  },

  phoneNumbers: {
    list: (limit = 50, offset = 0) =>
      request<Page<PhoneNumber>>("/phone-numbers", { query: { limit, offset } }),
  },

  billing: {
    usage: () => request<CurrentUsage>("/billing/usage"),
    quota: () => request<QuotaStatus>("/billing/quota"),
    history: () => request<Usage[]>("/billing/history"),
    plans: () => request<Plan[]>("/billing/plans"),
  },
};
