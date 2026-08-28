# Free-First Public Recovery Demo

## Summary

Build one genuinely complete public flow:

```text
Razorpay test checkout
  -> failure or abandonment detection
  -> deterministic diagnosis plus Luna explanation
  -> recovery link or email
  -> successful retry
  -> live timeline and scoreboard
```

Use four live integrations only:

| Integration | Decision | Role |
|---|---|---|
| Razorpay Orders, Payments, and Webhooks | Build now | Real test transactions, authoritative payment state, failure detection, and recovery verification |
| First-party checkout telemetry | Build now | Detect abandonment before Razorpay has a payment event |
| OpenAI Responses API with `gpt-5.6-luna` | Build now; only paid API | Structured case explanations and aggregate anomaly analysis |
| Resend Email API and webhooks | Build now on the free tier | Deliver recovery emails to allowlisted reviewers and report delivery status |

Razorpay test mode is available without real money. Resend currently includes a free usage tier.
Luna supports structured output and is the only paid API approved for this build.

## API decisions

### Build now

- Use Razorpay `POST /v1/orders`, `GET /v1/payments/:id`, and
  `GET /v1/orders/:id/payments`.
- Subscribe to `payment.failed`, `payment.captured`, and `order.paid`; retain raw-body
  signature verification, inbox persistence, event-ID deduplication, and asynchronous processing.
- Build a signed internal recovery URL that reopens Razorpay Checkout for the original unpaid
  order. Do not use the Payment Links API: its test-mode limit makes it unsuitable for an ongoing
  public demo.
- Add Resend `POST /emails` integration with the action idempotency key and a signed
  `/webhooks/resend` receiver for sent, delivered, bounced, complained, clicked, and failed events.
- Replace the deterministic cohort transport in live-demo mode with an OpenAI Responses adapter
  using `gpt-5.6-luna`, low reasoning, structured JSON output, no tools, `store=false`, and bounded
  output.

### Retain only in the clearly labelled Scenario Lab

- Razorpay Subscriptions and Invoices: useful later, but they add setup without strengthening the
  chosen checkout recovery loop.
- WhatsApp, SMS, and PSTN voice: trials are temporary, recipient-restricted, and unsuitable for a
  sustainable public demo.
- Simulated voice dialogue: retain for regression coverage and the Scenario Lab, but remove it from
  live navigation and recovery ladders.
- Synthetic five-surface scoreboard: preserve it for evaluation, but never mix it with live-demo
  metrics.

### Drop from the active roadmap

- Firebase Cloud Messaging: there is no merchant mobile app or meaningful device audience.
- External CRM or CDP integration: use demo-session customer records and consent instead.
- External helpdesk integration: use the existing internal escalation queue.
- Razorpay Customers API: unnecessary for the controlled public checkout flow.
- Razorpay Payment Links API: the internal signed recovery route can reuse the original order.

## Implementation changes

### Public demo and data flow

- Add `POST /demo/sessions` to create a short-lived session and Razorpay test order. Return the
  session token, Razorpay public key, order ID, fixed amount, currency, and expiry.
- Add `POST /demo/sessions/{id}/checkout-events` accepting `checkout_opened`,
  `payment_attempt_started`, `checkout_dismissed`, and `checkout_completed`.
- Create `CHECKOUT_ABANDON` after 30 seconds of inactivity following dismissal only when no payment
  failure exists for the order. A Razorpay `payment.failed` event takes precedence and creates
  `PAYMENT_FAILURE`; never create both for the same attempt.
- Add `GET /recover/{signed_token}` to reopen Checkout against the original unpaid order. Tokens
  expire after 30 minutes and bind the session, order, amount, and merchant.
- Extend Razorpay normalization so `payment.captured` and `order.paid` close and attribute the
  matching case regardless of webhook order or duplication.
- Add `GET /demo/sessions/{id}` returning current state, case, provider statuses, recovery URL,
  timeline, and operational metrics. Poll it every two seconds while a session is active.

### Live planning and actions

- Add live-only ladders for `PAYMENT_FAILURE` and `CHECKOUT_ABANDON`: an immediate in-app recovery
  link followed by email after 30 seconds in demo time.
- Exclude `silent_retry`, WhatsApp, SMS, and voice from live mode. A failed one-time payment requires
  another customer-authorized Checkout attempt.
- Keep the existing simulation ladders unchanged for reproducibility.
- Introduce provider-neutral Razorpay, email, and model adapter protocols. Default to real adapters
  in `live_demo` mode and simulator adapters only in `simulation` mode.
- Record provider request IDs, responses, latency, retries, and costs in the append-only audit spine.

### Luna's role

- Add a `CaseInsight` structured result containing `summary`, `probable_cause`, `evidence`,
  `recommended_next_step`, and `confidence`.
- Send only error classifications, payment method, amount band, and aggregate provider fields.
  Never send email, phone, customer name, or raw identifiers.
- Luna explains a case but cannot approve an action, override a gate, alter consent, or compose
  unregistered customer-facing text.
- Retain deterministic Tier 1 diagnosis and guardrails as the authority.
- Continue cohort anomaly analysis when enough real events accumulate; do not fabricate a cohort
  threshold for a single demo session.
- On timeout, budget exhaustion, or schema failure, persist the failed call and show the
  deterministic explanation without blocking recovery.

### Email safety and free-tier protection

- Accept an optional recipient only when it matches `LEAKPROOF_DEMO_EMAIL_ALLOWLIST`; otherwise
  return an email preview with status `preview_only`.
- Limit delivery to one recovery email per case and five emails per allowlisted address per day.
- Add Redis limits of ten sessions per IP per hour and bounded checkout-event ingestion per session.
- Refuse to start `live_demo` mode unless the Razorpay public key begins with `rzp_test_`; never
  permit live payment credentials in the public demo.
- Track Resend daily and monthly usage locally, warn at 80%, and switch further sends to
  `quota_blocked` previews before exceeding the free tier.
- Redact recipient addresses in API responses, timelines, and logs.

### Dashboard and measurement

- Make **Live Demo** the default dashboard, with source badges for Browser Telemetry, Razorpay
  Webhook, Luna, and Resend.
- Show detected amount, current case state, diagnosis, AI explanation, gate decision, recovery
  action, provider receipt, recovered amount, and end-to-end latency.
- Use an operational live scoreboard: cases detected, recovered cases, amount recovered, recovery
  rate, median recovery time, provider failures, and Luna cost.
- Hide treatment-versus-holdout lift for public sessions because a single-session demo cannot
  support causal measurement.
- Move the existing synthetic scoreboard under **Scenario Lab** with an unmistakable synthetic
  banner.

## Test and acceptance plan

- Unit-test provider adapters for timeouts, malformed responses, authentication failures, retries,
  and idempotency.
- Contract-test representative Razorpay and Resend webhook payloads, signatures, duplicate
  delivery, and out-of-order success and failure events.
- Test abandonment precedence: dismissal without an attempt creates abandonment; a failed payment
  creates only payment failure; later success closes the existing case.
- Test Luna structured output, PII exclusion, token and cost recording, invalid-schema retry, and
  deterministic fallback.
- Test public abuse controls, expired recovery tokens, modified amounts, non-allowlisted recipients,
  email quota exhaustion, and session isolation.
- Preserve the complete simulator regression suite.

Sandbox acceptance requires a reviewer to:

1. Start a public demo session.
2. Dismiss or fail Razorpay Checkout.
3. See the case and Luna explanation appear.
4. Open the signed recovery route or receive an allowlisted Resend email.
5. Complete a successful Razorpay test payment.
6. See the same case close and the live scoreboard update without manual database changes.

## Assumptions

- The public deployment has a stable HTTPS URL because Razorpay cannot deliver webhooks to
  localhost.
- A Razorpay test account, test API keys, and webhook secret are available.
- A domain can be verified with Resend; public users cannot send email to arbitrary recipients.
- OpenAI billing is enabled for `gpt-5.6-luna`; no other paid API is permitted.
- PostgreSQL, Redis, Celery, and the existing simulator remain in place.
- Subscription, invoice, mandate, WhatsApp, SMS, and voice integrations are explicitly deferred
  rather than presented as live features.
