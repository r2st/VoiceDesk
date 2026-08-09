/**
 * Response shapes mirroring `backend/app/schemas`.
 *
 * Hand-maintained rather than generated: the surface the dashboard actually
 * consumes is a fraction of the OpenAPI document, and a narrow hand-written
 * set is easier to read at a call site than a generated one. When the backend
 * schemas change, these change with them.
 */

export type PlanTier = "starter" | "growth" | "business" | "enterprise";
export type BusinessStatus = "trial" | "active" | "suspended" | "cancelled";
export type UserRole = "owner" | "admin" | "supervisor" | "viewer";
export type AgentStatus = "draft" | "active" | "paused";
export type AgentUseCase =
  | "appointment_booking"
  | "payment_reminder"
  | "lead_qualification"
  | "order_status"
  | "customer_support";
export type CallDirection = "inbound" | "outbound";
export type CallStatus =
  | "queued"
  | "ringing"
  | "in_progress"
  | "completed"
  | "no_answer"
  | "busy"
  | "failed"
  | "cancelled"
  | "blocked_dnd"
  | "blocked_calling_hours";
export type CallResolution =
  | "resolved"
  | "unresolved"
  | "escalated"
  | "handed_off"
  | "pending";
export type Sentiment = "positive" | "neutral" | "negative";
export type SpeakerRole = "caller" | "agent" | "system" | "human";
export type Language = "hi" | "en" | "ta" | "te" | "mr" | "bn" | "kn";

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  expires_at: string;
}

export interface User {
  id: string;
  business_id: string;
  email: string;
  full_name: string;
  phone: string | null;
  role: UserRole;
  is_active: boolean;
  last_login_at: string | null;
  created_at: string;
}

export interface Business {
  id: string;
  name: string;
  slug: string;
  phone: string;
  email: string;
  industry: string | null;
  gstin: string | null;
  city: string | null;
  state: string | null;
  plan: PlanTier;
  status: BusinessStatus;
  trial_ends_at: string | null;
  settings_json: Record<string, unknown>;
  created_at: string;
}

export interface WindowMetrics {
  total_calls: number;
  total_duration_sec: number;
  avg_duration_sec: number;
  total_billable_minutes: number;
  total_cost_paise: number;
  resolved_calls: number;
  resolution_rate: number;
  inbound_calls: number;
  outbound_calls: number;
  answered_calls: number;
  answer_rate: number;
  positive_sentiment: number;
  negative_sentiment: number;
  handed_off_calls: number;
}

export interface DashboardSummary extends WindowMetrics {
  period_days: number;
  /** Serialised under the `from` alias by the backend. */
  from: string;
  to: string;
  active_agents: number;
  deltas: {
    total_calls: number;
    resolution_rate: number;
    avg_duration_sec: number;
  };
}

export interface TimeseriesPoint {
  date: string;
  total_calls: number;
  inbound_calls: number;
  outbound_calls: number;
  answered_calls: number;
  avg_duration_sec: number;
  resolution_rate: number;
  billable_minutes: number;
  cost_paise: number;
  positive_sentiment: number;
  negative_sentiment: number;
}

export interface IntentCount {
  intent: string;
  count: number;
}

export interface CallAnalytics {
  date_from: string;
  date_to: string;
  agent_id: string | null;
  series: TimeseriesPoint[];
  totals: WindowMetrics;
  languages: Record<string, number>;
  intents: IntentCount[];
}

export interface AgentLeaderboardEntry {
  agent_id: string | null;
  agent_name: string;
  total_calls: number;
  avg_duration_sec: number;
  resolved_calls: number;
  resolution_rate: number;
  avg_sentiment_score: number;
  billable_minutes: number;
}

export interface VoiceAgent {
  id: string;
  business_id: string;
  name: string;
  description: string | null;
  use_case: AgentUseCase;
  status: AgentStatus;
  language: Language;
  supported_languages: string[];
  voice_id: string;
  persona: string;
  greeting: string | null;
  fallback_message: string | null;
  flow_json: Record<string, unknown>;
  flow_version: number;
  max_call_duration_sec: number;
  handoff_confidence_threshold: number;
  whatsapp_handoff_enabled: boolean;
  recording_enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface Call {
  id: string;
  business_id: string;
  agent_id: string | null;
  phone_number_id: string | null;
  direction: CallDirection;
  status: CallStatus;
  caller_number: string;
  callee_number: string;
  provider: string;
  provider_call_id: string | null;
  scheduled_at: string | null;
  started_at: string | null;
  answered_at: string | null;
  ended_at: string | null;
  duration_sec: number;
  billable_minutes: number;
  cost_paise: number;
  language: Language | null;
  detected_languages: string[];
  sentiment: Sentiment | null;
  sentiment_score: number | null;
  resolution: CallResolution;
  primary_intent: string | null;
  avg_confidence: number | null;
  summary: string | null;
  dnd_checked: boolean;
  consent_announced: boolean;
  opted_out: boolean;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
}

export interface ConversationTurn {
  id: string;
  call_id: string;
  turn_index: number;
  role: SpeakerRole;
  content: string;
  language: Language | null;
  confidence: number | null;
  sentiment: Sentiment | null;
  detected_intent: string | null;
  flow_node_id: string | null;
  latency_ms: number | null;
  model_used: string | null;
  created_at: string;
}

export interface CallDetail extends Call {
  conversations: ConversationTurn[];
  has_recording: boolean;
  sentiment_trajectory: Record<string, unknown> | null;
}

export interface PhoneNumber {
  id: string;
  business_id: string;
  agent_id: string | null;
  number: string;
  provider: string;
  provider_number_id: string | null;
  region: string | null;
  status: "provisioning" | "active" | "released" | "failed";
  inbound_enabled: boolean;
  outbound_enabled: boolean;
  monthly_rent_paise: number;
  created_at: string;
}

export interface Plan {
  tier: PlanTier;
  name: string;
  monthly_fee_paise: number;
  per_minute_paise: number;
  included_minutes: number;
  max_agents: number | null;
  max_languages: number | null;
  features: string[];
  annual_fee_paise: number;
  is_custom: boolean;
}

export interface QuotaStatus {
  month: string;
  plan: PlanTier;
  minutes_used: number;
  included_minutes: number;
  remaining_minutes: number;
  overage_minutes: number;
  utilization_pct: number;
  in_overage: boolean;
}

export interface Usage {
  id: string;
  business_id: string;
  month: string;
  plan_id: PlanTier;
  minutes_used: number;
  included_minutes: number;
  overage_minutes: number;
  calls_count: number;
  base_fee_paise: number;
  overage_paise: number;
  number_rent_paise: number;
  amount_paise: number;
  tax_paise: number;
  total_paise: number;
  currency: string;
  is_finalized: boolean;
  finalized_at: string | null;
  invoice_number: string | null;
  created_at: string;
}

export interface CurrentUsage {
  usage: Usage;
  plan: Plan;
  quota: QuotaStatus;
  projected_total_paise: number;
}
