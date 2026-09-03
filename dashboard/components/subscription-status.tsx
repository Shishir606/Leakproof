import type { SubscriptionProjection } from "@/lib/demo-types";
import { label, money } from "@/lib/format";

export function SubscriptionStatus({ subscription }: { subscription: SubscriptionProjection }) {
  return (
    <section className="invoice-status" aria-label="Subscription recovery status">
      <div><span>Subscription</span><strong>{label(subscription.provider_status)}</strong></div>
      <div><span>Affected cycle</span><strong>{subscription.cycle_resolved ? label(subscription.cycle_status ?? "resolved") : "Awaiting correlation"}</strong></div>
      <div><span>Outstanding</span><strong>{money(subscription.outstanding_balance_paise)}</strong></div>
      <div><span>Recurring retries</span><strong>Razorpay-owned</strong><small>{subscription.retry_count} observed state change(s)</small></div>
      <div><span>Recovery</span><strong>{label(subscription.disposition)}</strong><small>Method repair and old-invoice collection are tracked separately.</small></div>
    </section>
  );
}
