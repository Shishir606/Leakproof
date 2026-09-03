// Compile-time exhaustiveness checks, included by the existing tsc gate.
import { assertNever } from "./resource-types";
import type { ProviderSignal, ResourceRecoveryBootstrap, ResourceSessionCreated } from "./resource-types";

export function signalKind(signal: ProviderSignal): string {
  switch (signal.kind) {
    case "risk": return signal.leak_type;
    case "state": return signal.state;
    case "recovery": return signal.settlement;
    default: return assertNever(signal);
  }
}
export function recoveryPurpose(bootstrap: ResourceRecoveryBootstrap): string {
  switch (bootstrap.purpose) {
    case "order_checkout": return bootstrap.razorpay_order_id;
    case "invoice_hosted_payment": return bootstrap.redirect_url;
    case "subscription_method_update": return bootstrap.subscription_id;
    default: return assertNever(bootstrap);
  }
}
export function primaryIdentity(session: ResourceSessionCreated): string {
  switch (session.primary_entity_type) {
    case "order": return session.razorpay_order_id;
    case "invoice": return session.primary_entity_id;
    case "subscription": return session.primary_entity_id;
    default: return assertNever(session);
  }
}
