# Leakproof: All Five Recovery Surfaces Implementation Plan

**Prepared:** 2 September 2026  
**Status:** Proposed extension after the current payment-recovery release  
**Audience:** Implementers, reviewers, and Razorpay recruiters

## 1. Outcome

Extend Leakproof from one reliable payment-failure demo into a coherent Razorpay test-mode recovery
workbench for all five existing leak types:

1. `PAYMENT_FAILURE` — preserve the current provider-verified flow.
2. `CHECKOUT_ABANDON` — stabilize and prove the existing telemetry-driven flow.
3. `INVOICE_OVERDUE` — detect unpaid/expired invoices and recover through the original hosted
   invoice payment surface.
4. `SUBSCRIPTION_HALT` — detect pending or halted subscriptions and recover through an explicit
   payment-method update or customer-authorized charge.
5. `MANDATE_BROKEN` — distinguish a revoked, expired, or cancelled recurring authorization from a
   generic subscription failure, then guide the payer through re-authorization.

The finished demo should let a reviewer choose a scenario, watch a verified provider event become a
case, see a bounded recovery decision, take the correct customer-authorized action, and watch a
later Razorpay event close the same case. No scenario may imply that browser telemetry, a model
answer, or an email click proves money was recovered.

## 2. Current baseline

The repository already has more reusable infrastructure than the public demo exposes:

- All five `LeakType` values, case states, attribution windows, simulator coverage, scoreboards, and
  synthetic outcome measurement exist.
- The signed Razorpay webhook inbox is durable, deduplicated, asynchronously processed, and
  replay-safe.
- `subscription.pending` and `subscription.halted` have an initial normalizer, but are not connected
  to the live-demo session, recovery, projection, or acceptance paths.
- Checkout abandonment already has browser telemetry, a delayed server recheck, precedence beneath
  `payment.failed`, and original-order recovery. It needs a deterministic rehearsal and clearer
  product entry point rather than a second implementation.
- Invoice and mandate signals exist only in Scenario Lab.
- The live contracts and dashboard deliberately allow only `PAYMENT_FAILURE` and
  `CHECKOUT_ABANDON`.
- The current provider adapter understands orders and payments only. It has no typed invoice,
  subscription, or recurring-authorization operations.

This extension should preserve the working payment path and evolve the shared live-demo model. It
should not fork the codebase into five independent pipelines.

## 3. Product truth and capability labels

Use these labels consistently in the API, UI, README, demo script, and acceptance artifacts:

| Label | Required evidence |
|---|---|
| `LIVE_PROVIDER_VERIFIED` | A real Razorpay test-mode resource and a signed webhook or successful API re-fetch drive both detection and closure. |
| `LIVE_TELEMETRY_PROVIDER_RECONCILED` | First-party telemetry detects risk, then Razorpay API/webhook truth confirms the resource is still unpaid and later confirms recovery. This is the checkout-abandonment label. |
| `CONTRACT_VERIFIED` | Official payload fixtures and adapter contract tests pass, but the account/test environment cannot emit the scenario reliably. |
| `SIMULATED_END_TO_END` | The existing deterministic Scenario Lab path only. |

Do not upgrade a capability label merely because its code path exists. Each surface earns its label
only after a sanitized provider rehearsal artifact passes validation.

## 4. Scope boundaries

### Build

- One scenario chooser and one consistent recovery timeline for all five leak types.
- Typed Razorpay adapters for invoices and subscriptions in addition to the existing order/payment
  adapter.
- Normalization, correlation, precedence, success reconciliation, recovery routes, and acceptance
  evidence for each surface.
- Customer-authorized recovery only: reopen Checkout, open the original invoice URL, or update the
  subscription payment method.
- Resend email as an optional, allowlisted follow-up using the existing quotas and delivery audit.
- Deterministic diagnosis and policy remain authoritative. Luna explains or proposes within the
  existing bounded contracts and may not invoke provider operations.

### Do not build in this extension

- Live-mode charges, settlements, payouts, refunds, or production customer outreach.
- Autonomous debits or an app-owned retry loop for recurring payments. Razorpay already owns the
  subscription retry schedule; Leakproof must not create a double-charge path.
- WhatsApp, SMS, or PSTN integrations.
- A generic workflow engine, CRM, or identity platform.
- A fake “mandate revoked” button presented as provider-verified evidence.

## 5. Target experience

Replace the single-path start page with a **Recovery Lab** containing five scenario cards. Each card
shows its evidence level, setup requirement, expected duration, and the action the reviewer will
take.

```text
Choose scenario
  -> create or select a Razorpay test resource
  -> receive telemetry or a signed provider event
  -> normalize and correlate the provider entity
  -> create/update exactly one recovery case
  -> deterministic diagnosis -> optional Luna explanation -> guardrail verdict
  -> present the surface-specific customer-authorized recovery
  -> verify Razorpay success
  -> close the same case and cancel pending contact
  -> export a sanitized acceptance artifact
```

The five cards should read as follows:

| Scenario | Detection proof | Recovery action | Closure proof |
|---|---|---|---|
| Payment failure | `payment.failed` or current payment API verification | Retry the original order in Checkout | Captured payment verification, `payment.captured`, or `order.paid` |
| Checkout abandonment | Signed session telemetry plus unpaid-order recheck | Reopen the original order in Checkout | Captured payment verification, `payment.captured`, or `order.paid` |
| Invoice overdue | `invoice.expired`, or a reconciler-confirmed issued invoice beyond the configured threshold with `amount_due > 0` | Open the original Razorpay invoice `short_url` | `invoice.paid` or API-confirmed `amount_due == 0`; partial payment updates risk but does not close |
| Subscription halt | `subscription.pending` or `subscription.halted` | Update the payment method; expose manual charge only as an operator instruction | `subscription.charged` plus active/activated state, correlated to the failed cycle |
| Mandate broken | Recurring-payment/subscription evidence with an allowlisted mandate-invalid reason | Re-authorize or change the recurring payment method | New authorization followed by active subscription and a successful correlated charge |

## 6. Shared architecture changes

### 6.1 Generalize the demo session without breaking the payment path

Keep `demo_sessions` and add a forward-only migration with:

- `scenario_type`: one of the five `LeakType` values; default `PAYMENT_FAILURE` for old rows.
- `primary_entity_type`: `order`, `invoice`, or `subscription`.
- `primary_entity_id`: provider identifier; encrypted or tokenized if the threat model requires it.
- `setup_state`: `CREATING`, `READY`, `ACTION_REQUIRED`, `AT_RISK`, `RECOVERED`, `FAILED`, or
  `EXPIRED`.
- `capability_evidence`: the current provenance label earned by this run.

Make `razorpay_order_id` nullable only after the generic entity fields are populated for every
existing row. Keep it during the migration as a compatibility field for the existing checkout
routes, then remove it only in a later release if doing so still adds value.

Add a `provider_entities` table:

- `merchant_id`, `session_id`, `provider`, `entity_type`, `provider_entity_id`, and `role`.
- `root_entity_type` and `root_entity_id` for payment -> order, payment -> invoice, and invoice ->
  subscription correlation.
- Current provider status and safe non-PII metadata.
- Unique `(merchant_id, provider, entity_type, provider_entity_id)` constraint.
- Indexes for session lookup and root-entity lookup.

This correlation table prevents case lookup from depending on whichever identifier happens to be
present in one webhook payload.

### 6.2 Extend provider contracts, not business logic conditionals

Split the current Razorpay boundary into small protocols that a single `RazorpayProvider` may
implement:

- `OrderPaymentProvider`: existing create/fetch/list behavior.
- `InvoiceProvider`: create test invoice, issue it, fetch it, and return its hosted `short_url`.
- `SubscriptionProvider`: create/fetch subscription and expose the data needed to start or update
  payment authorization.

Define typed entities (`Invoice`, `Subscription`, `ProviderEntityStatus`) that contain only fields
the application uses. Reject malformed IDs, amount/currency mismatches, unexpected states, and
missing root relationships at the adapter boundary. Preserve the existing retry, timeout, request
ID, and safe provider-call audit behavior.

### 6.3 Normalize into explicit risk and recovery signals

Do not overload `PaidSignal` for every product. Introduce:

- `RiskSignal`: the existing `NormalizedSignal` semantics with explicit provider entity/root IDs.
- `RecoverySignal`: merchant, leak type, provider entity/root IDs, amount recovered or amount still
  due, currency, evidence source, and occurrence time.
- `EntityStateSignal`: non-terminal state changes such as invoice partial payment or subscription
  pending -> halted.

Normalize the following events:

| Product | Risk/state events | Recovery events |
|---|---|---|
| Checkout/payment | Existing browser dismissal and `payment.failed` | `payment.captured`, `order.paid` |
| Invoice | `invoice.expired`, `invoice.partially_paid` | `invoice.paid` |
| Subscription | `subscription.pending`, `subscription.halted`, relevant recurring `payment.failed` | `subscription.charged`, `subscription.activated` |
| Mandate | A recurring failure with a verified mandate-invalid reason; correlated subscription halt may add evidence | Successful re-authorization plus correlated `subscription.charged`/`subscription.activated` |

Raw webhook bodies may contain customer name, email, phone, and address. Keep them in the protected
webhook inbox only for the minimum necessary retention period; never copy them into case events,
model input, logs, projections, or acceptance artifacts.

### 6.4 Define deduplication and precedence before implementation

Use stable case roots:

- Payment failure and abandonment: session + order, as today.
- Invoice overdue: merchant + invoice ID.
- Subscription halt: merchant + subscription ID + billing cycle/invoice ID.
- Mandate broken: merchant + subscription/authorization root + affected billing cycle.

Precedence rules:

1. `PAYMENT_FAILURE` replaces `CHECKOUT_ABANDON` for the same order/attempt.
2. `MANDATE_BROKEN` replaces `SUBSCRIPTION_HALT` for the same cycle only when provider evidence
   contains an allowlisted mandate-invalid reason. A generic failure must stay a subscription halt.
3. `invoice.partially_paid` updates `amount_at_risk` to `amount_due`; it must not create another
   case or close the original one.
4. Success received before a delayed failure is retained as terminal truth when its provider event
   time and entity version are newer.
5. Duplicate webhooks may create one inbox receipt but no duplicate cases, actions, emails, timers,
   or terminal events.

Create table-driven tests for every pair of competing events before wiring UI behavior.

### 6.5 Make recovery tokens entity-aware

Version the signed recovery token and bind it to:

- session, merchant, scenario, entity type, provider entity ID, amount/currency where applicable,
  expiry, and a purpose value;
- one of `order_checkout`, `invoice_hosted_payment`, or `subscription_method_update`.

`GET /recover/{token}` must re-fetch provider state before returning a recovery bootstrap:

- order: return the existing Checkout material only if the original order is unpaid;
- invoice: return a server-approved redirect to the invoice's current provider `short_url` only if
  `amount_due > 0` and the invoice is payable;
- subscription/mandate: return Checkout configuration for payment-method update only if the
  subscription remains pending/halted and belongs to the bound session.

Never embed the provider URL or configuration in the token. Do not treat opening the URL as a
successful recovery.

### 6.6 Reuse the action and safety spine

Add live action types with explicit semantics:

- `recovery_link`: existing order recovery.
- `invoice_payment_link`: opens the original Razorpay invoice.
- `subscription_method_update`: customer-authorized payment method update.
- `email_link`: existing allowlisted Resend follow-up, parameterized by surface.
- `merchant_review`: internal, non-customer-facing fallback for malformed or unsupported provider
  states.

Remove `silent_retry` from every live recurring ladder. It may remain in Scenario Lab. For live
subscriptions, “Razorpay retry scheduled” is an observed provider state, not an action executed by
Leakproof.

All actions retain one case/step identity, one idempotency key, gate-before-send behavior, stop
rules, quiet-hour rules, provider-call audit, and cancellation after verified recovery.

## 7. Surface implementation tracks

### Track A — Checkout abandonment: stabilize and prove

The underlying path exists. Complete it as a first-class scenario:

1. Add `scenario_type=CHECKOUT_ABANDON` session creation while keeping the existing creation route
   backward compatible.
2. Make dismissal rehearsal deterministic: record dismissal, show the countdown/recheck state, and
   visibly distinguish browser evidence from the Razorpay unpaid-order confirmation.
3. Ensure refresh, duplicate `ondismiss`, broker handoff failure, and Beat redispatch still produce
   one case.
4. Preserve `payment.failed` precedence and cancel the abandonment timer if a failure or success is
   observed.
5. Add a one-command automated contract rehearsal and one browser-driven acceptance capture.

**Acceptance gate:** a fresh reviewer can dismiss Checkout, see one abandonment case, reopen the
original order, complete payment, and see the same case close with verified recovered amount.

### Track B — Invoice overdue

1. Add typed create, issue, and fetch invoice operations. The demo setup must disable provider
   notification when Leakproof is responsible for the optional allowlisted follow-up.
2. Persist the invoice relationship and safe amount fields when the test invoice is created.
3. Detect `invoice.expired` from signed webhooks. Add a scheduled reconciliation path for issued
   invoices that cross a configured overdue threshold and still have `amount_due > 0`; the API
   re-fetch is the authority for this derived detection.
4. On `invoice.partially_paid`, append an immutable event, update outstanding risk, and retain the
   case. On `invoice.paid`, close and attribute only the remaining/recovered amount according to a
   documented partial-payment rule.
5. Use the provider's original invoice `short_url`; do not create a second order or Payment Link.
6. Add invoice-specific email wording and show due amount, aging bucket, partial payments, and the
   provider state in the projection without exposing customer details.

**Acceptance gate:** an actual Razorpay test invoice produces one provider-correlated case; partial
payment does not close it; full payment closes the same case; duplicate or out-of-order invoice
events converge on the same final projection.

### Track C — Subscription halt

1. Add a reusable test plan configured by environment, then create a subscription against it. Do
   not create unbounded plans per demo run.
2. Store subscription, current invoice/cycle, plan amount, and safe retry counters in
   `provider_entities`.
3. Treat `subscription.pending` as early risk and `subscription.halted` as escalation of the same
   cycle case. Do not create one case per retry.
4. Correlate recurring `payment.failed` payloads and invoices to the subscription before diagnosis.
5. Present Razorpay's payment-method update flow. Leakproof may explain that Razorpay owns automatic
   retries; it may not initiate an extra debit.
6. Close only after a correlated successful charge and an active/activated subscription state.
   `subscription.activated` without proof of the affected invoice/charge should update state but not
   over-attribute money.

**Acceptance gate:** consecutive test-mode failures move one subscription case from pending to
halted, payment-method recovery returns it to active, a correlated charge closes the same case, and
no Leakproof action can double-charge it.

### Track D — Broken mandate

This is a specialized recurring-recovery track, not a second subscription implementation.

1. Start with an evidence spike against the actual Razorpay test account. Record the precise event
   and payload fields emitted for cancelled, revoked, or expired authorization across the enabled
   recurring method. Freeze sanitized fixtures from those observations.
2. Create an allowlist of provider reasons that unambiguously mean authorization is invalid. Never
   infer `MANDATE_BROKEN` from `insufficient_funds`, a generic bank decline, or a plain halted state.
3. Reclassify a same-cycle `SUBSCRIPTION_HALT` case to `MANDATE_BROKEN` when stronger evidence
   arrives, refresh diagnosis, cancel obsolete actions, and plan the payment-method update ladder.
4. The recovery action must require explicit payer authorization. Do not store card, VPA, bank
   account, or authorization secrets.
5. Close after a new authorization is provider-confirmed and the affected subscription/cycle has a
   successful charge. Track “authorization repaired” and “revenue recovered” as separate events.

**Acceptance gate:** a real test-mode provider event proves the classification and a later
re-authorization plus successful charge closes the same case. If the test account cannot reliably
emit the required evidence, ship this track as `CONTRACT_VERIFIED`, keep its interactive scenario
disabled, and state the limitation plainly.

## 8. API and contract changes

Evolve the public endpoints as follows:

- `POST /demo/sessions` accepts an optional `scenario_type`; omission retains today’s payment demo.
- `GET /demo/scenarios` returns availability, evidence label, prerequisites, setup state, and safe
  reviewer instructions for all five surfaces.
- `POST /demo/sessions/{id}/provider-resources` is not public. Scenario resource creation happens as
  part of session creation or through an operator-authenticated setup route.
- `GET /demo/sessions/{id}` returns a generalized projection with provider entity, outstanding
  amount, surface-specific state, recovery action, and timeline.
- `GET /recover/{token}` returns a discriminated `RecoveryBootstrap` union for order, invoice, or
  subscription recovery.
- Existing checkout telemetry and payment verification routes stay intact and reject incompatible
  scenario types.
- Acceptance export gains scenario-specific checks but remains token-protected and identifier-free.

Replace narrow literals in Python and TypeScript with shared exhaustive unions covering all five
leak types and all live action types. Contract tests must fail if either side omits a newly supported
variant.

## 9. Dashboard changes

1. Add the five-card Recovery Lab before creating a session.
2. Keep one timeline component and change only the scenario instructions and recovery CTA.
3. Show two badges near every case: detection source and recovery-verification source.
4. Show setup progress separately from recovery progress so a reviewer never mistakes “test
   resource created” for “risk detected.”
5. For subscriptions, display Razorpay-owned retry state separately from Leakproof-owned actions.
6. For mandates, display `Authorization repaired` separately from `Charge recovered`.
7. Keep Scenario Lab in a separate route and never merge its figures with provider-backed results.
8. Update the headline only as capability gates pass. Until then, say “one provider-verified loop;
   four expansion tracks in progress.”

## 10. Delivery sequence

### Milestone 0 — Provider capability spike

- Confirm Invoices and Subscriptions are enabled in the Razorpay test account.
- Register invoice and subscription webhook events on the stable HTTPS endpoint.
- Capture sanitized real payloads for invoice expiry/partial/full payment, subscription
  pending/halted/charged/activated, and mandate-invalid behavior.
- Record unsupported or non-deterministic test-mode behaviors before designing UI promises.

**Exit:** an evidence matrix identifies which tracks can earn `LIVE_PROVIDER_VERIFIED` and which
must remain `CONTRACT_VERIFIED`.

### Milestone 1 — Multi-entity foundation

- Migration, provider entity correlation, generalized contracts, typed provider adapters, generic
  recovery signals, and entity-aware signed tokens.
- Keep all current payment tests green throughout the migration.

**Exit:** the existing payment-failure and abandonment flows work unchanged through the generalized
model.

### Milestone 2 — Checkout abandonment acceptance

- Scenario entry point, visible delayed recheck, deterministic acceptance capture, and release gate.

**Exit:** both current order scenarios have passing provider-reconciled artifacts.

### Milestone 3 — Invoice vertical slice

- Provider adapter -> webhook normalization -> one case -> invoice recovery -> partial/full payment
  reconciliation -> dashboard -> artifact.

**Exit:** the invoice acceptance gate passes end to end.

### Milestone 4 — Subscription vertical slice

- Reusable plan, subscription setup, pending/halted lifecycle, safe update-method recovery,
  charged/activated reconciliation, UI, and artifact.

**Exit:** the subscription acceptance gate passes without an app-owned debit.

### Milestone 5 — Mandate specialization

- Evidence allowlist, precedence over subscription halt, re-authorization state, separated
  authorization/revenue outcomes, UI, and artifact.

**Exit:** provider-verified if the test environment proves it; otherwise contract-verified with the
interactive scenario disabled.

### Milestone 6 — Portfolio release gate

- Cross-surface chaos tests, redaction scan, clean/reused database verification, dashboard build,
  capability matrix validation, five sanitized artifacts, and a 2–3 minute recruiter demo.

**Exit:** the repository makes no broader claim than its captured evidence supports.

## 11. Test and verification matrix

Every surface must cover:

- valid and invalid signatures;
- webhook without provider event ID using a stable fingerprint;
- duplicate delivery and worker redelivery;
- success before failure, failure before success, and stale event after terminal state;
- provider timeout, 429, 5xx, authentication failure, and malformed response;
- amount, currency, merchant, session, and root-entity mismatch;
- expired/tampered recovery token and token used for the wrong scenario;
- Luna unavailable/invalid/budget-exhausted without blocking recovery;
- Resend preview-only, allowlist, quotas, duplicate delivery, bounce, and complaint;
- cancellation of all pending customer contact after verified recovery;
- absence of PII, secrets, signed URLs, and provider IDs from public projections and artifacts.

Add surface-specific cases:

| Surface | Required edge cases |
|---|---|
| Checkout abandonment | Duplicate dismissal, dismissal then failure, dismissal then success, failed broker enqueue, expired session |
| Invoice | Partial payments, amount-due update, expired then paid, paid then late expired, cancelled/non-payable invoice |
| Subscription | Several retries in one cycle, pending -> active, pending -> halted, halted -> active, next cycle after old case closure |
| Mandate | Generic failure must not classify as mandate broken, strong evidence reclassifies one existing case, authorization repaired without charge, charge after re-authorization |

Provider fixtures must originate from official examples or sanitized test-account captures and be
labelled with their source. Hand-authored fixtures cannot be the sole evidence for a live claim.

## 12. Acceptance artifacts

Produce one sanitized JSON artifact per surface with:

- scenario and capability label;
- provider resource type, never the raw identifier;
- risk event source and signature-verification result;
- deduplication and correlation checks;
- case type, deterministic diagnosis, optional Luna status, and guardrail verdict;
- recovery action type and whether it required customer authorization;
- terminal provider event/API verification;
- recovered/outstanding amount and latency;
- redaction and no-duplicate-action checks.

The checkout-abandonment artifact uses the telemetry-specific provenance label. A mandate artifact
cannot pass as live when it uses only a replayed fixture.

## 13. Recruiter demo script

Keep the live walkthrough under three minutes:

1. Open the five-card Recovery Lab and point out the evidence badges.
2. Run payment failure or checkout abandonment interactively to show the fastest full recovery.
3. Open a prepared provider-verified invoice case and pay the remaining balance, emphasizing
   partial-payment handling and same-case reconciliation.
4. Open a prepared halted subscription and show that Razorpay owns retries while Leakproof guides a
   safe payment-method update.
5. Show mandate evidence becoming a more specific diagnosis than a generic halt, then show
   authorization repair and revenue recovery as separate facts.
6. Finish on the immutable timeline and acceptance bundle: duplicate/out-of-order events converge,
   AI cannot move money, and every revenue claim has provider proof.

The strongest recruiting story is not “five integrations.” It is “one safety and audit architecture
that correctly handles five different payment-product state machines.”

## 14. Definition of done

- [ ] All five scenarios appear in the capability API and Recovery Lab with honest evidence labels.
- [ ] Current payment-failure behavior and its release gate have no regressions.
- [ ] Checkout abandonment has a passing browser-driven, provider-reconciled artifact.
- [ ] Invoice expiry/overdue, partial payment, full payment, and out-of-order events converge on one
      case.
- [ ] Subscription pending/halted/charged/activated events converge on one case per affected cycle.
- [ ] Mandate classification requires provider-specific evidence and never relies on a generic
      recurring failure.
- [ ] No live action can autonomously debit or double-charge a customer.
- [ ] Every recovery token is short-lived, purpose-bound, and rechecks Razorpay before use.
- [ ] All pending contacts are cancelled after verified recovery.
- [ ] Python tests, type checks, dashboard build, migrations from zero, reused-database verification,
      public-bundle secret scan, and redaction tests pass.
- [ ] Each enabled live scenario has a sanitized Razorpay test-mode acceptance artifact.
- [ ] README and dashboard claims exactly match those artifacts.

## 15. Planning decisions that must not be reopened during implementation

1. Keep one case/event/action spine for all surfaces.
2. Use a provider-entity correlation table instead of identifier heuristics scattered across
   normalizers.
3. Reuse original provider resources; do not create a second order or generic Payment Link for
   invoice recovery.
4. Let Razorpay own subscription retries; Leakproof only observes and guides authorized recovery.
5. Treat broken mandate as an evidence-qualified specialization of recurring failure.
6. Separate authorization repair from recovered revenue.
7. Gate public capability claims with actual sanitized provider rehearsals.

## 16. External references to revalidate at implementation time

- [Razorpay webhook behavior and verification](https://razorpay.com/docs/webhooks/)
- [Razorpay invoice webhook payloads](https://razorpay.com/docs/webhooks/invoices/)
- [Razorpay invoice APIs](https://razorpay.com/docs/api/payments/invoices/)
- [Razorpay subscription webhook payloads](https://razorpay.com/docs/webhooks/subscriptions/)
- [Razorpay subscription test workflow](https://razorpay.com/docs/payments/subscriptions/test/)
- [Razorpay subscription retry behavior](https://razorpay.com/docs/payments/subscriptions/payment-retries/)

Provider behavior, account entitlements, event availability, and test-mode limits must be checked
again immediately before implementing each provider-backed milestone.
