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
