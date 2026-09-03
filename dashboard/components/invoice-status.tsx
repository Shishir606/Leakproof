import type { InvoiceProjection } from "@/lib/demo-types";
import { label, money } from "@/lib/format";

export function InvoiceStatus({ invoice }: { invoice: InvoiceProjection }) {
  return <section className="checkout-receipt invoice-status" aria-label="Invoice balance and status">
    <div><span>Business due date</span><strong>{new Date(invoice.business_due_at).toLocaleString("en-IN")}</strong></div>
    <div><span>Business status</span><strong>{invoice.business_overdue ? "Overdue" : invoice.disposition === "paid" ? "Settled" : "Not overdue"} · {label(invoice.aging_bucket)}</strong></div>
    <div><span>Provider status</span><strong>{label(invoice.provider_status)}</strong></div>
    <div><span>Provider expiry</span><strong>{invoice.provider_expires_at ? new Date(invoice.provider_expires_at).toLocaleString("en-IN") : "No provider expiry"}</strong></div>
    <div><span>Original detected balance</span><strong>{invoice.detected_balance_paise === null ? "Awaiting detection" : money(invoice.detected_balance_paise)}</strong></div>
    <div><span>Current outstanding</span><strong>{money(invoice.outstanding_balance_paise)}</strong></div>
    <div><span>Total paid on invoice</span><strong>{money(invoice.amount_paid_paise)}</strong></div>
    <p role="status">{invoice.disposition === "merchant_review" ? "Payment is unavailable. Contact the merchant to review this invoice." : invoice.disposition === "provider_retry" ? "Provider verification is delayed. Recovery is paused while we retry." : invoice.disposition === "paid" ? "Invoice settled. No further payment is needed." : invoice.amount_paid_paise > 0 ? "Partial payment received. This case stays open for the remaining balance." : "Waiting for the business due date or a verified payment. The balance refreshes automatically."}</p>
  </section>;
}
