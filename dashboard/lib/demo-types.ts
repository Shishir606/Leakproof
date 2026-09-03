import type { DataProvenance, LeakType, SetupState, ResourceSessionCreated, ResourceRecoveryBootstrap } from "./resource-types";

export type CheckoutEventType =
  | "checkout_opened"
  | "payment_attempt_started"
  | "checkout_dismissed"
  | "checkout_completed";

export type DemoSession = Exclude<ResourceSessionCreated, { primary_entity_type: "subscription" }>;
export type RecoveryBootstrap = Exclude<ResourceRecoveryBootstrap, { purpose: "subscription_method_update" }>;

export type InvoiceProjection = {
  provider_status: string;
  business_due_at: string;
  business_overdue: boolean;
  aging_bucket: string;
  provider_expires_at: string | null;
  detected_balance_paise: number | null;
  outstanding_balance_paise: number;
  amount_paid_paise: number;
  recovered_paise: number;
  disposition: "payable" | "merchant_review" | "paid" | "provider_retry";
  last_checked_at: string | null;
  partial_payment: boolean;
};

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
  leak_type: LeakType;
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
  action_type: "recovery_link" | "invoice_payment_link" | "subscription_method_update" | "email_link" | "merchant_review";
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

export type AbandonmentCheck = {
  status: "idle" | "waiting" | "provider_recheck" | "provider_retry" | "provider_pending" | "confirmed" | "payment_failure" | "recovered";
  due_at: string | null;
  browser_dismissed_at: string | null;
  unpaid_confirmed: boolean;
};

export type DemoSessionProjection = {
  invoice: InvoiceProjection | null;
  abandonment_check: AbandonmentCheck;
  scenario_type: LeakType;
  primary_entity_type: "order" | "invoice" | "subscription";
  setup_state: SetupState;
  capability_evidence: DataProvenance;
  data_provenance: DataProvenance;
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

export type CheckoutPaymentVerificationReceipt = {
  verified: true;
  duplicate: boolean;
  state: "RECOVERED";
  payment_status: "captured";
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
