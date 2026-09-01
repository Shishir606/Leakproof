export type CheckoutEventType =
  | "checkout_opened"
  | "payment_attempt_started"
  | "checkout_dismissed"
  | "checkout_completed";

export type DemoSession = {
  session_id: string;
  session_token: string;
  razorpay_key_id: string;
  razorpay_order_id: string;
  amount_paise: number;
  currency: string;
  expires_at: string;
  email_mode: "allowlisted" | "preview_only";
};

export type RecoveryBootstrap = Omit<DemoSession, "session_token" | "email_mode">;

export type DemoSessionState =
  | "CREATED"
  | "CHECKOUT_OPEN"
  | "AT_RISK"
  | "RECOVERED"
  | "EXPIRED";

export type CaseInsight = {
  summary: string;
  probable_cause: string;
  evidence: string[];
  recommended_next_step: string;
  confidence: number;
};

export type DemoCaseProjection = {
  case_id: string;
  leak_type: "PAYMENT_FAILURE" | "CHECKOUT_ABANDON";
  state: string;
  deterministic_diagnosis: null | {
    rule_id: string | null;
    failure_class: string;
    confidence: number;
  };
  insight: CaseInsight | null;
  insight_status: "pending" | "succeeded" | "fallback";
};

export type ProviderStatus = {
  provider: "razorpay" | "openai" | "resend";
  operation: string;
  status: string;
  request_id: string | null;
  latency_ms: number | null;
  attempts: number | null;
  error_class: string | null;
};

export type RecoveryAction = {
  action_id: string | null;
  action_type: "recovery_link" | "email_link";
  status: string;
  scheduled_for: string;
  executed_at: string | null;
  gate_verdict: string | null;
  provider_receipt_id: string | null;
};

export type TimelineItem = {
  kind: string;
  source: "browser" | "razorpay" | "openai" | "resend" | "leakproof";
  occurred_at: string;
  payload: Record<string, unknown>;
};

export type DemoSessionProjection = {
  data_provenance: "LIVE_PROVIDER_VERIFIED" | "SIMULATED_END_TO_END" | "ARCHITECTURE_READY";
  session_id: string;
  state: DemoSessionState;
  amount_paise: number;
  currency: string;
  expires_at: string;
  email_mode: "allowlisted" | "preview_only";
  case: DemoCaseProjection | null;
  recovery_url_available: boolean;
  recovery_path: string | null;
  gate_verdict: string | null;
  recovery_actions: RecoveryAction[];
  provider_statuses: ProviderStatus[];
  timeline: TimelineItem[];
  end_to_end_latency_seconds: number | null;
  metrics: {
    cases_detected: number;
    recovered_cases: number;
    recovered_amount_paise: number;
    recovery_rate: number;
    median_recovery_time_seconds: number | null;
    provider_failures: number;
    luna_cost_paise: number;
  };
  environment_metrics: {
    cases_detected: number;
    recovered_cases: number;
    recovered_amount_paise: number;
    recovery_rate: number;
    median_recovery_time_seconds: number | null;
    provider_failures: number;
    luna_cost_paise: number;
  };
};

export type CheckoutEvent = {
  client_event_id: string;
  event_type: CheckoutEventType;
  occurred_at: string;
  metadata: {
    attempt_id?: string;
    dismissed_by?: "customer" | "browser" | "unknown";
    sdk_version?: string;
  };
};

export type ApiErrorPayload = {
  error?: {
    code?: string;
    message?: string;
    retryable?: boolean;
  };
};

export type RazorpaySuccess = {
  razorpay_payment_id: string;
  razorpay_order_id: string;
  razorpay_signature: string;
};

export type RazorpayFailure = {
  error?: {
    description?: string;
    reason?: string;
  };
};

export type RazorpayOptions = {
  key: string;
  order_id: string;
  amount: number;
  currency: string;
  name: string;
  description: string;
  handler: (response: RazorpaySuccess) => void;
  modal: {
    confirm_close: boolean;
    escape: boolean;
    ondismiss: () => void;
  };
  notes: Record<string, string>;
  retry: { enabled: boolean };
  theme: { color: string; backdrop_color: string };
};

export interface RazorpayCheckout {
  open(): void;
  on(event: "payment.submit", callback: () => void): void;
  on(event: "payment.failed", callback: (response: RazorpayFailure) => void): void;
}

declare global {
  interface Window {
    Razorpay?: new (options: RazorpayOptions) => RazorpayCheckout;
  }
}
