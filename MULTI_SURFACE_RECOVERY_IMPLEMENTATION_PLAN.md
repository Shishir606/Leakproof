# Leakproof: All Five Recovery Surfaces Implementation Plan

**Prepared:** 2 September 2026  
**Status:** Capability investigation complete 3 September 2026; account prerequisites and implementation remain open
**Audience:** Implementers, reviewers, and Razorpay recruiters

## 1. Outcome

Extend Leakproof from one reliable payment-failure demo into a coherent Razorpay test-mode recovery
workbench for all four existing leak types:

1. `PAYMENT_FAILURE` — preserve the current provider-verified flow.
2. `CHECKOUT_ABANDON` — stabilize and prove the existing telemetry-driven flow.
3. `INVOICE_OVERDUE` — detect aged, unpaid invoices; recover payable invoices through the original
   hosted surface and route expired/non-payable invoices to merchant review.
4. `SUBSCRIPTION_HALT` — detect pending or halted subscriptions and recover through an explicit
   payment-method update or customer-authorized charge.

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

- One scenario chooser and one consistent recovery timeline for all four leak types.
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

Replace the single-path start page with a **Recovery Lab** containing four scenario cards. Each card
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

The four cards should read as follows:

| Scenario | Detection proof | Recovery action | Closure proof |
|---|---|---|---|
| Payment failure | `payment.failed` or current payment API verification | Retry the original order in Checkout | Captured payment verification, `payment.captured`, or `order.paid` |
| Checkout abandonment | Signed session telemetry plus unpaid-order recheck | Reopen the original order in Checkout | Captured payment verification, `payment.captured`, or `order.paid` |
| Invoice overdue | Re-fetch confirms an aged `issued`/`partially_paid` invoice with `amount_due > 0`; `invoice.expired` is a separate non-payable risk state | Original `short_url` only while payable; otherwise merchant review | Verified full settlement of that invoice; partial payments reduce risk and credit only new paid amounts |
| Subscription halt | `subscription.pending` or `subscription.halted`, resolved to the unpaid invoice | Customer payment-method update; eligible old-invoice Dashboard charge is operator-only and unsupported for domestic cards | Captured settlement of the affected invoice; subscription reactivation is a separate service-state fact |

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
| Mandate | Evidence-qualified recurring failure or linked token cancellation; see section 17.3 | Verified authorization repair; only affected-invoice settlement proves revenue |

Raw webhook bodies may contain customer name, email, phone, and address. Keep them in the protected
webhook inbox only for the minimum necessary retention period; never copy them into case events,
model input, logs, projections, or acceptance artifacts.

### 6.4 Define deduplication and precedence before implementation

**One obligation, one case, one monetary ledger, regardless of event surface.** These are design
requirements, not safeguards already implemented for new surfaces.

- Namespace every identity by merchant, provider, and test/live mode. Use the provider invoice ID
  as the canonical obligation when present, including subscription-generated invoices. Map its
  order and every payment attempt to that same obligation. A standalone order keeps the existing
  session/order binding and additionally has one provider-order identity across sessions.
- A subscription is a parent of multiple invoice obligations, not one monetary obligation. A token
  is authorization evidence, not another receivable. Preserve these relationships in
  `provider_entities`; one `root_entity_id` alone must not collapse all subscription cycles.
- Enforce unique obligation -> case ownership in the database. Classify the existing case by
  `SUBSCRIPTION_HALT` (linked recurring failure) >
  `INVOICE_OVERDUE` (aging/non-payability) > `PAYMENT_FAILURE` > `CHECKOUT_ABANDON`. Precedence
  applies only within the same proven obligation; no cross-customer or cross-cycle promotion.
- Resolve payment -> invoice -> subscription before dispatching actions or money attribution.
  Missing/ambiguous links go into a non-attributable reconciliation state; do not guess from
  customer, matching amounts, `paid_count`, or the most recent invoice. Provisional observations
  do not send contact. If an order case already exists when its invoice relationship arrives,
  attach it atomically to the canonical obligation, preserving case/action history. Conflicting
  legacy owners require a reviewed merge/alias with one counted owner and all duplicate actions
  cancelled before eligibility is restored; never leave two independently credited cases.
- Reclassification preserves case ID, arm, detection time, attribution window, and existing credit.
  Cancel obsolete pending actions; use the existing case/step idempotency and shared contact limits.
  A token affecting several cycles may annotate each actual unpaid invoice, but authorization
  repair is deduplicated at subscription/token level and does not spawn one contact per invoice.
- Add a settlement ledger uniquely keyed by `(merchant, provider, mode, payment_id)` and linked to
  one obligation. `payment.captured`, `order.paid`, `invoice.paid`, `invoice.partially_paid`,
  `subscription.charged`, webhook redelivery, and API reconciliation are observations of the same
  settlement. A cumulative amount or a new webhook ID is never a second monetary credit. Resolve
  payment IDs before monetary attribution; unresolved invoice-paid evidence can stop contact and
  show settlement pending reconciliation without guessing credit.
- At detection, freeze the unpaid balance and baseline paid amount. Reduce risk on partial payment;
  credit only unique, post-detection captured payments applied to that balance, capped by the
  original unpaid amount, and never add the cumulative invoice total again at final settlement.
  Example: 100 total, 20 already paid at detection, then 30 and 50 paid -> 80 maximum recovery,
  regardless of how many surface events arrive. Keep existing last-touch/window/organic rules;
  provider settlement alone does not establish incremental causal lift or a Leakproof action.
- Serialize reconciliation per obligation and insert settlement credit transactionally under the
  unique constraint. Success arriving before risk produces no retrospective recovery credit.
  Preserve full-settlement truth against delayed failures/expiry; re-fetch conflicting snapshots.
  Webhook envelope `created_at` is event time; entity `created_at` is often resource creation time,
  not state-transition time. Razorpay does not promise a general entity version: do not invent one.
- Zero-value registration invoices, authorization payments/refunds, method updates, `activated`,
  and token confirmation carry no recovered-invoice revenue. A later-cycle charge cannot close an
  earlier unpaid invoice. Subscription active/authorization repaired and invoice settled must be
  separate projection facts. Cancel contact on settlement even if subscription state lags.

Extend existing duplicate/order-success/replay tests with these event permutations, partial-payment
arithmetic, concurrent inserts, cross-session ownership, late relationship resolution, ambiguous
cycles, and two unpaid cycles. Today `uq_attribution_case` alone and the customer/amount fallback in
`record_paid_signal` do **not** satisfy this gate; bypass that fallback for new provider surfaces.

### 6.5 Make recovery tokens entity-aware

Version the signed recovery token and bind it to:

- session, merchant, scenario, entity type, provider entity ID, amount/currency where applicable,
  expiry, and a purpose value;
- one of `order_checkout`, `invoice_hosted_payment`, or `subscription_method_update`.

`GET /recover/{token}` must re-fetch provider state before returning a recovery bootstrap:

- order: return the existing Checkout material only if the original order is unpaid;
- invoice: return a server-approved redirect to the invoice's current provider `short_url` only if
  `amount_due > 0` and the invoice is payable;
- subscription/mandate: return a documented card-change Checkout configuration or provider-hosted
  method-update surface only for a verified supported method/state and bound subscription. The
  card-change flag is not a universal mandate API; use section 17.4's method restrictions. A
  cancelled/expired/completed subscription has no same-subscription restart recovery CTA.

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
3. Detect `invoice.expired` as non-payable risk requiring merchant review. Reconcile `issued` and
   `partially_paid` invoices against an application-owned aging threshold and positive due amount;
   `expire_by` is a payment cutoff, not a business due date. Apply section 17.1's payable-state gate.
4. On `invoice.partially_paid`, append an immutable event, update outstanding risk, and retain the
   case. On `invoice.paid`, close and attribute only the remaining/recovered amount according to a
   documented partial-payment rule.
5. Use the original invoice `short_url` only while payable. Expired invoices cannot be revived by
   extending `expire_by`; do not create a replacement order, invoice, or Payment Link in recovery.
6. Add invoice-specific email wording and show due amount, aging bucket, partial payments, and the
   provider state in the projection without exposing customer details.

**Acceptance gate:** an actual Razorpay test invoice produces one provider-correlated case; partial
payment does not close it; full payment closes the same case; duplicate or out-of-order invoice
events converge on the same final projection.

### Track C — Subscription halt

1. Add a reusable test plan configured by environment, then create a subscription against it. Do
   not create unbounded plans per demo run.
2. Store subscription, exact affected invoice/cycle, actual invoice amount/due/currency, and safe
   retry counters in `provider_entities`. Plan price alone is not the outstanding balance.
3. Treat `subscription.pending` as early risk and `subscription.halted` as escalation of the same
   cycle case. Do not create one case per retry.
4. Correlate recurring `payment.failed` payloads and invoices to the subscription before diagnosis.
5. Present Razorpay's payment-method update flow. Leakproof may explain that Razorpay owns automatic
   retries; it may not initiate an extra debit.
6. Settle/close the monetary obligation only after verified full payment of the affected invoice;
   track active/activated service state separately. A method update from pending can charge the last
   invoice, while halted -> active does not guarantee old arrears were charged. Never settle an old
   cycle from a future cycle's charge or activation alone.

**Acceptance gate:** Dashboard-controlled test failures move one invoice case from pending to
halted; method recovery and exact-invoice settlement are independently proven. If eligible manual
arrears charging is unavailable, show active-with-arrears and merchant review rather than claiming
recovery. No Leakproof action can double-charge it.

## 8. API and contract changes

Evolve the public endpoints as follows:

- `POST /demo/sessions` accepts an optional `scenario_type`; omission retains today’s payment demo.
- `GET /demo/scenarios` returns availability, evidence label, prerequisites, setup state, and safe
  reviewer instructions for all four surfaces.
- `POST /demo/sessions/{id}/provider-resources` is not public. Scenario resource creation happens as
  part of session creation or through an operator-authenticated setup route.
- `GET /demo/sessions/{id}` returns a generalized projection with provider entity, outstanding
  amount, surface-specific state, recovery action, and timeline.
- `GET /recover/{token}` returns a discriminated `RecoveryBootstrap` union for order, invoice, or
  subscription recovery.
- Existing checkout telemetry and payment verification routes stay intact and reject incompatible
  scenario types.
- Acceptance export gains scenario-specific checks but remains token-protected and identifier-free.

Replace narrow literals in Python and TypeScript with shared exhaustive unions covering all four
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

### Milestone 0 — Provider capability investigation (complete; rehearsal prerequisites open)

- Official APIs, event schemas, state/action decisions, and documentation gaps checked on
  2026-09-03; see section 17 and the progress log's capability matrix/manual checklist.
- Existing adapter reused for GET probes: Orders readable, Invoices readable with zero returned
  items, Plans and Subscriptions return HTTP 401. Cause/entitlements remain unresolved.
- No provider resource creation, webhook registration change, debit, or recipient contact occurred.
- Account activation/access resolution, webhook selection, resource setup, and signed lifecycle
  captures belong to a later explicitly authorized rehearsal, not this read-only investigation.

**Exit met:** supported implementation decisions and unmet prerequisites are recorded. New surfaces
remain simulated; no account lifecycle or new contract evidence label was earned. Milestone 1 may
build against documented contracts, with interactive provider claims blocked until rehearsal.

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
| Invoice | Partial-payment deltas, overdue-but-payable vs expired, no expired recovery CTA, paid then late expired, contradictory expired/paid snapshots re-fetched, cancelled/deleted/draft, registration invoice excluded |
| Subscription | Several retries in one invoice, payment missing from pending/halted payload, two unpaid invoices with the same paid_count, pending -> active, halted -> active with arrears, next-cycle charge does not close old cycle |

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

- [ ] All four scenarios appear in the capability API and Recovery Lab with honest evidence labels.
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
5. Separate authorization repair from recovered revenue.
6. Gate public capability claims with actual sanitized provider rehearsals.

## 16. Official sources checked 3 September 2026

- [Razorpay webhook behavior and verification](https://razorpay.com/docs/webhooks/)
- [Razorpay invoice webhook payloads](https://razorpay.com/docs/webhooks/invoices/)
- [Razorpay invoice APIs](https://razorpay.com/docs/api/payments/invoices/)
- [Razorpay subscription webhook payloads](https://razorpay.com/docs/webhooks/subscriptions/)
- [Razorpay subscription test workflow](https://razorpay.com/docs/payments/subscriptions/test/)
- [Razorpay subscription retry behavior](https://razorpay.com/docs/payments/subscriptions/payment-retries/)
- [Invoice state semantics](https://razorpay.com/docs/payments/invoices/states/)
- [State-specific invoice updates](https://razorpay.com/docs/api/payments/invoices/update/)
- [Issue invoice and minimum expiry](https://razorpay.com/docs/api/payments/invoices/issue/)
- [Subscription APIs](https://razorpay.com/docs/api/payments/subscriptions/)
- [Subscription invoice relationship and billing fields](https://razorpay.com/docs/api/payments/subscriptions/fetch-invoices/)
- [Subscription entity](https://razorpay.com/docs/api/payments/subscriptions/entity/)
- [Subscription states](https://razorpay.com/docs/payments/subscriptions/states/)
- [Manual charge restrictions](https://razorpay.com/docs/payments/subscriptions/manually-charge-card/)
- [Subscription method/account limitations](https://razorpay.com/docs/payments/subscriptions/faqs/)
- [Recurring Payments token and registration events](https://razorpay.com/docs/api/payments/recurring-payments/webhooks/)
- [eMandate error reasons](https://razorpay.com/docs/payments/recurring-payments/emandate/errors/)
- [eMandate token fetch semantics](https://razorpay.com/docs/api/payments/recurring-payments/emandate/tokens/)
- [UPI token access prerequisites](https://razorpay.com/docs/api/payments/recurring-payments/upi/tokens/)
- [Webhook delivery/deduplication](https://razorpay.com/docs/webhooks/best-practices/)

Provider behavior, account entitlements, event availability, and test-mode limits must be checked
again immediately before implementing each provider-backed milestone.

## 17. Capability decisions and unresolved account gates

Evidence terms in this section are deliberately separate: **documented** means supported by the
official references; **account observed** means the bounded read actually succeeded; **unverified**
means no account lifecycle capture. The dated, identifier-free probe record is
[`docs/evidence/razorpay-capability-readonly-2026-09-03.json`](docs/evidence/razorpay-capability-readonly-2026-09-03.json).
It is an access observation, not an acceptance artifact or a webhook fixture.

### 17.1 Invoice aging, expiry, and actions

There is no documented `overdue` provider status or `invoice.overdue` webhook. Define aging in
Leakproof: at a recorded reconciliation time, a valid `issued`/`partially_paid` invoice has positive
`amount_due` and has passed an explicit merchant due time, or `issued_at + configured age threshold`
when no due time exists. Record the rule and threshold. Missing timestamps do not default to overdue.
Do not substitute `expire_by` or `billing_end` for that business rule. Expiry disables payment;
aging alone does not. Sources: [invoice states](https://razorpay.com/docs/payments/invoices/states/),
[invoice fields](https://razorpay.com/docs/api/payments/invoices/fetch-with-id/).

| Current provider state | Supported recovery decision |
|---|---|
| `draft` | Setup incomplete, not an overdue case. An operator can complete/issue it in a later authorized setup; no payer link now. |
| `issued` | Original hosted invoice URL while positive balance, valid currency/ownership, and no elapsed expiry. An operator may extend `expire_by` before expiry; Leakproof recovery does not mutate it. |
| `partially_paid` | Same hosted URL for remaining balance while unexpired; keep the same case. Do not promise expiry extension here: documented updates allow notes only. |
| `paid` | Stop recovery/contact and reconcile actual settlement. Do not offer another payment. |
| `expired` | Non-payable; merchant review. No reopen, reissue, or expiry-extension recovery API is supported by the reviewed state/update contract. |
| `cancelled` / `deleted` | No payment CTA and no automatic replacement resource; merchant review/non-recovered terminal disposition. |
| Unknown, unavailable, or contradictory | Hold the CTA, re-fetch/reconcile, then merchant review if unresolved. A URL, null/zero balance, or expired timestamp alone does not establish settlement. |

An elapsed `expire_by` with a still-issued snapshot is conservatively non-payable until re-fetched.
An apparent expired -> paid event sequence is a reconciliation/ordering test, not a promised ability
to pay after expiry. Use `status=paid`, consistent due/paid amounts, and linked settlement evidence;
zero `amount_due` alone may describe a registration or non-revenue resource. State/action restrictions
come from [invoice updates](https://razorpay.com/docs/api/payments/invoices/update/) and
[invoice lifecycle](https://razorpay.com/docs/payments/invoices/states/).

### 17.2 Available API/event contracts and failed-cycle identity

| Product | Documented contract | Scope decision |
|---|---|---|
| Invoices | `GET /v1/invoices`, `GET /v1/invoices/:id`; create `POST /v1/invoices`; issue `POST /v1/invoices/:id/issue`; update `PATCH /v1/invoices/:id`; cancel `POST /v1/invoices/:id/cancel`; delete `DELETE /v1/invoices/:id`; notification and item APIs also exist | Implement only typed create/issue/fetch/list needed for later setup/reconciliation; no notifications or replacements in recovery. Invoice API creation is non-GST. |
| Invoice events | `invoice.partially_paid`, `invoice.paid`, `invoice.expired` | No overdue event; periodic reads also detect unsupported/terminal changes. |
| Plans/subscriptions | Create/list/fetch plans and subscriptions; subscription-link creation; update, pending-update fetch/cancel, cancel, pause/resume, offer link/remove; `GET /v1/invoices?subscription_id=:sub_id` | Typed fetch/list/correlation first; one configured reusable plan in later setup. Resume API is for paused subscriptions, not an arrears retry API. No public test-charge/manual-charge endpoint appears in this API index. |
| Subscription events | `subscription.authenticated`, `.activated`, `.charged`, `.completed`, `.updated`, `.pending`, `.halted`, `.paused`, `.resumed`, `.cancelled` | Normalize lifecycle without inventing `subscription.failed` or relying on an undocumented `subscription.expired` event. Only actual affected-invoice payments carry revenue. |
| Payments | Existing fetch/list-order-payments; `payment.failed`, `payment.captured`, `order.paid`; authorization is distinct | Reuse transport/inbox; add explicit invoice/subscription relationships before dispatch. |

Sources: [Invoice APIs](https://razorpay.com/docs/api/payments/invoices/),
[invoice events](https://razorpay.com/docs/webhooks/invoices/),
[Subscription APIs](https://razorpay.com/docs/api/payments/subscriptions/),
[subscription events](https://razorpay.com/docs/webhooks/subscriptions/).

Use `payment.invoice_id` when supplied, fetch that invoice, and verify `invoice.subscription_id`,
`order_id`, currency and amount. Otherwise paginate subscription invoices and resolve the affected
unpaid invoice from preserved provider relationships and billing-period evidence. Store invoice ID
as the cycle identity, with `billing_start`/`billing_end` as context; `current_start`/`current_end`
describe the current subscription period and can advance while arrears remain. Do not infer a
unique failed cycle from `paid_count` (successful cycles), `auth_attempts` (retry count), `charge_at`
(next attempt), plan price, or subscription creation time. The official pending/halted examples may
contain only a subscription, so absence of payment is valid. With multiple possible unpaid invoices,
retain subscription state and require reconciliation before case attribution or contact. Sources:
[subscription invoice API](https://razorpay.com/docs/api/payments/subscriptions/fetch-invoices/),
[entity fields](https://razorpay.com/docs/api/payments/subscriptions/entity/).

### 17.4 Recovery restrictions, manual tests, and documentation gaps

Use Checkout `subscription_id` plus `subscription_card_change=true` for documented card update.
Provider-hosted payment-method update supports a documented matrix: card -> card/UPI/eMandate;
UPI -> card; eMandate -> card. Do not promise UPI -> UPI/eMandate or eMandate -> UPI/eMandate on
this flow. Method availability remains an account prerequisite. Razorpay owns retries; neither
general subscription PATCH nor paused-subscription resume is a payment-method repair/charge API.
Source: [payment retries and method update](https://razorpay.com/docs/payments/subscriptions/payment-retries/).

The test guide broadly describes successful card changes causing charge and activation; the retry
guide distinguishes pending's last-invoice charge from halted arrears requiring manual collection.
Therefore no method-update callback or active state settles arrears without invoice/payment proof.
Manual invoice `Attempt Charge` is a Dashboard action for an issued invoice and is explicitly
unsupported for domestic cards. Treat arrears recovery as unavailable when the account/method lacks
that action, rather than adding an autonomous debit or replacement Payment Link. Sources:
[manual charge](https://razorpay.com/docs/payments/subscriptions/manually-charge-card/),
[subscription states](https://razorpay.com/docs/payments/subscriptions/states/).

Deterministic subscription test charges require Dashboard **Charge this now** with success/failure
selection; four consecutive failures demonstrate pending -> halted. Halted **Issue Invoice** creates
an additional unpaid cycle without charging. **Attempt Charge** on a selected older invoice tests
arrears separately. The test guide limits subsequent card debits to three days after token creation
and subscription-update testing to resources without subsequent test charges. Keep separate test
resources for those tests; do not assume the update restriction either proves or rules out the
card-change recovery flow. Sources: [test workflow](https://razorpay.com/docs/payments/subscriptions/test/),
[method/account FAQ](https://razorpay.com/docs/payments/subscriptions/faqs/).

For invoices, setup can use API or Dashboard; aging is an app threshold, expiry needs provider time
to pass, and partial/full payment needs a human on the hosted invoice. Issuance documents at least
15 minutes of future expiry, so do not promise an instant expiry button. GST-specific invoice setup
is Dashboard work outside the minimal non-GST API demo. Sources:
[issue constraints](https://razorpay.com/docs/api/payments/invoices/issue/),
[Invoice APIs](https://razorpay.com/docs/api/payments/invoices/).

Unresolved account gates: explain Plans/Subscriptions HTTP 401 with the account owner; confirm test
product/method entitlements; select a reusable plan; inspect enabled HTTPS webhook events and
signature delivery; verify hosted-method update and notifications under controlled test-recipient
settings; capture invoice, subscription, and mandate lifecycle evidence. A configured webhook secret
and HTTPS base URL do not prove event registration or delivery. No Dashboard settings were inspected
or changed, and no recipient or provider support was contacted in this investigation. The progress
log contains the manual checklist for a later authorized rehearsal.
