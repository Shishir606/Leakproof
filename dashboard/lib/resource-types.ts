// Shared foundation. Availability is supplied by /demo/scenarios, not by enum membership.
export const LEAK_TYPES = ["PAYMENT_FAILURE", "CHECKOUT_ABANDON", "SUBSCRIPTION_HALT", "INVOICE_OVERDUE", "MANDATE_BROKEN"] as const;
export const ENTITY_TYPES = ["order", "invoice", "subscription", "payment", "token"] as const;
export const SETUP_STATES = ["CREATING", "READY", "ACTION_REQUIRED", "FAILED", "EXPIRED"] as const;
export const RECOVERY_PURPOSES = ["order_checkout", "invoice_hosted_payment", "subscription_method_update"] as const;
export const DATA_PROVENANCE = ["LIVE_PROVIDER_VERIFIED", "LIVE_TELEMETRY_PROVIDER_RECONCILED", "CONTRACT_VERIFIED", "SIMULATED_END_TO_END", "ARCHITECTURE_READY"] as const;
export type LeakType = typeof LEAK_TYPES[number];
export type EntityType = typeof ENTITY_TYPES[number];
export type SetupState = typeof SETUP_STATES[number];
export type RecoveryPurpose = typeof RECOVERY_PURPOSES[number];
export type DataProvenance = typeof DATA_PROVENANCE[number];
export type EntityRef = { entity_type: EntityType; entity_id: string };
export type ObligationRef = { entity_type: "order" | "invoice"; entity_id: string };
export type ProviderScope = { merchant_id: string; provider: "razorpay"; mode: "test" | "live" };
type SignalBase = {
  scope: ProviderScope;
  entity: EntityRef;
  root: EntityRef | null;
  obligation: ObligationRef | null;
  source: "razorpay_webhook" | "razorpay_api" | "browser_provider_reconciled";
  occurred_at: string;
};
export type RiskSignal = SignalBase & {
  kind: "risk";
  leak_type: LeakType;
  customer_id: string;
  amount_due_paise: number;
  baseline_paid_paise: number;
  currency: string;
  mandate_evidence: "qualified" | null;
};
export type EntityStateSignal = SignalBase & {
  kind: "state";
  state: "pending" | "halted" | "active" | "authorization_repaired" | "cancelled" | "expired" | "partially_paid" | "reconciliation_required";
  amount_due_paise: number | null;
  currency: string | null;
};
export type RecoverySignal = SignalBase & {
  kind: "recovery";
  leak_type: LeakType | null;
  payment_id: string | null;
  amount_paise: number;
  amount_due_paise: number | null;
  currency: string;
  settlement: "captured_payment" | "full_settlement" | "authorization_repaired";
};
export type ProviderSignal = RiskSignal | EntityStateSignal | RecoverySignal;
export type OrderRecoveryBootstrap = {
  purpose: "order_checkout";
  session_id: string;
  razorpay_key_id: string;
  razorpay_order_id: string;
  amount_paise: number;
  currency: string;
  expires_at: string;
};
export type ResourceRecoveryBootstrap = OrderRecoveryBootstrap | {
  purpose: "invoice_hosted_payment";
  session_id: string;
  redirect_url: string;
  expires_at: string;
} | {
  purpose: "subscription_method_update";
  session_id: string;
  razorpay_key_id: string;
  subscription_id: string;
  subscription_card_change: true;
  expires_at: string;
};
export type ScenarioCapability = {
  scenario_type: LeakType;
  primary_entity_type: "order" | "invoice" | "subscription";
  enabled: boolean;
  capability_evidence: DataProvenance;
  reason: string | null;
};
export const SCENARIO_ENTITIES = {
  PAYMENT_FAILURE: "order",
  CHECKOUT_ABANDON: "order",
  INVOICE_OVERDUE: "invoice",
  SUBSCRIPTION_HALT: "subscription",
  MANDATE_BROKEN: "subscription",
} as const satisfies Record<LeakType, ScenarioCapability["primary_entity_type"]>;
export function assertNever(value: never): never {
  throw new Error(`Unsupported contract variant: ${String(value)}`);
}

export type Invoice = {
  request_id: string | null;
  id: string;
  order_id: string | null;
  subscription_id: string | null;
  status: "draft" | "issued" | "partially_paid" | "paid" | "cancelled" | "expired" | "deleted";
  amount_paise: number;
  amount_paid_paise: number;
  amount_due_paise: number;
  currency: string;
  short_url: string | null;
};
export type Subscription = {
  request_id: string | null;
  id: string;
  plan_id: string;
  status: "created" | "authenticated" | "active" | "pending" | "halted" | "cancelled" | "completed" | "expired" | "paused";
  payment_method: "card" | "upi" | "emandate" | null;
  affected_invoice_id: string | null;
};
type ResourceSessionBase = {
  session_id: string;
  session_token: string;
  setup_state: SetupState;
  amount_paise: number;
  currency: string;
  expires_at: string;
  email_mode: "allowlisted" | "preview_only";
};
export type ResourceSessionCreated = ResourceSessionBase & ({
  primary_entity_type: "order";
  scenario_type: "PAYMENT_FAILURE" | "CHECKOUT_ABANDON";
  razorpay_order_id: string;
  razorpay_key_id: string;
} | {
  primary_entity_type: "invoice";
  scenario_type: "INVOICE_OVERDUE";
  primary_entity_id: string;
} | {
  primary_entity_type: "subscription";
  scenario_type: "SUBSCRIPTION_HALT" | "MANDATE_BROKEN";
  primary_entity_id: string;
});
