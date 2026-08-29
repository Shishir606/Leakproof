# Leakproof Live API Integration Blueprint

**Implementation window:** 29 August-4 September 2026
**Source of truth:** `API_INTEGRATION_PLAN.md`
**Purpose:** Convert the approved API decisions into an executable, testable delivery sequence without changing the existing simulator blueprint.

---

## 1. Delivery outcome

By the end of 4 September, a reviewer must be able to complete this flow without database edits or mocked browser responses:

```text
Create demo session and Razorpay test order
  -> open Razorpay Checkout
  -> dismiss Checkout or receive payment.failed
  -> create exactly one recovery case
  -> show deterministic diagnosis and Luna explanation
  -> expose a signed recovery URL immediately
  -> send one allowlisted Resend email after 30 seconds
  -> retry the original unpaid order successfully
  -> close the same case from payment.captured or order.paid
  -> update the live timeline and operational scoreboard
```

The existing simulator remains available under **Scenario Lab**. Live and synthetic data must never be combined in the same scoreboard.

## 2. Scope lock

### Integrations to implement

| Integration | Live responsibility | Completion signal |
|---|---|---|
| Razorpay Orders, Payments, and Webhooks | Create test orders, open Checkout, detect failures, confirm recovery | Original order is paid and the matching case closes |
| First-party browser telemetry | Detect dismissal-driven abandonment before a payment event exists | A dismissal with 30 seconds of inactivity creates one `CHECKOUT_ABANDON` case |
| OpenAI Responses API using `gpt-5.6-luna` | Produce a structured, non-authoritative `CaseInsight` | Valid insight is stored, or deterministic fallback is shown |
| Resend Email API and webhooks | Send an allowlisted recovery email and track delivery | Provider status appears in the session timeline |

### Explicitly out of scope

- Razorpay Payment Links, Customers, Subscriptions, and Invoices APIs
- Live WhatsApp, SMS, push, PSTN voice, and simulated voice in live navigation
- External CRM, CDP, and helpdesk integrations
- Silent retry for a one-time payment
- LLM-authored customer messages or LLM-controlled guardrails
- Causal lift or treatment-versus-holdout claims for public demo sessions

Any item in this list is deferred rather than partially exposed as a live feature.

## 3. Architecture and ownership boundaries

```text
Next.js Live Demo
  |-- POST /demo/sessions --------------------> Razorpay Orders adapter
  |-- POST /checkout-events -----------------> Telemetry service
  |-- GET /demo/sessions/{id} every 2s ------> Session projection
  |-- GET /recover/{token} ------------------> Original-order Checkout bootstrap
  |
FastAPI
  |-- /webhooks/razorpay --> verified inbox --> Celery processor
  |-- /webhooks/resend ---> verified inbox --> Celery processor
  |-- abandonment timer ---------------------> Celery worker
  |-- case insight task ----------------------> Luna adapter
  |-- recovery email action -----------------> Resend adapter
  |
PostgreSQL: sessions, events, cases, actions, provider receipts, insight/cost records
Redis: Celery broker plus IP, event, email, and quota counters
```

Keep the application a modular monolith. Provider-specific code belongs behind protocols; the case pipeline, guardrail gate, audit timeline, and measurement services remain provider-neutral.

### Adapter contracts

Implement these boundaries before provider code:

```python
class PaymentProvider(Protocol):
    def create_order(...): ...
    def fetch_payment(...): ...
    def list_order_payments(...): ...

class EmailProvider(Protocol):
    def send_recovery_email(...): ...

class CaseInsightProvider(Protocol):
    def explain_case(...): ...
```

Bind real adapters only when `LEAKPROOF_MODE=live_demo`; bind simulator adapters when `LEAKPROOF_MODE=simulation`. Tests use fakes implementing the same contracts.

## 4. Data contracts and persistence

Add one forward-only migration after the current migration head. Reuse `webhook_events`, `events`, `actions`, `actuator_receipts`, and `llm_calls` where their semantics already fit.

### New session projection

```text
demo_sessions
  id                         opaque public identifier
  merchant_id
  customer_id                generated demo identity; never email-derived
  razorpay_order_id          unique
  amount_paise
  currency
  state                      CREATED | CHECKOUT_OPEN | AT_RISK | RECOVERED | EXPIRED
  recipient_ciphertext       optional; never returned or logged in full
  recipient_hash             rate-limit and allowlist bookkeeping
  expires_at
  created_at
  updated_at
```

### Checkout telemetry

```text
checkout_events
  id
  session_id
  client_event_id            unique within session
  event_type                 checkout_opened | payment_attempt_started |
                             checkout_dismissed | checkout_completed
  occurred_at
  received_at
  metadata                   bounded allowlist only
```

The server sets a 30-second abandonment deadline only after `checkout_dismissed`. `payment.failed` wins over abandonment for the same attempt. The timer must re-read current order, payment, and case state before creating anything.

### Provider and delivery state

- Store Razorpay and Resend webhooks in the existing durable inbox using `provider + provider_event_key` uniqueness.
- Extend provider receipts, or add a provider-call table, to retain provider, operation, request ID, safe response metadata, latency, attempt number, status, and error class.
- Store Resend delivery events by provider email ID and event ID so duplicates are harmless.
- Store `CaseInsight` output separately from the authoritative deterministic diagnosis, or append it to the case timeline with an immutable typed payload.
- Never store raw API secrets, unredacted authorization headers, full webhook signatures, or full recipient addresses in audit payloads.

### Required `CaseInsight` schema

```json
{
  "summary": "string",
  "probable_cause": "string",
  "evidence": ["string"],
  "recommended_next_step": "string",
  "confidence": 0.0
}
```

Reject unknown fields, bound string and list lengths, and require `confidence` in `[0, 1]`.

## 5. API blueprint

### `POST /demo/sessions`

Input:

```json
{ "recipient": "optional@example.com" }
```

Behavior:

1. Apply the IP limit of 10 sessions per rolling hour.
2. Validate the optional recipient against `LEAKPROOF_DEMO_EMAIL_ALLOWLIST`.
3. Create the session and a fixed-amount Razorpay test order.
4. If the recipient is absent or not allowlisted, set email mode to `preview_only`; do not reject the demo.
5. Return only public Checkout material.

Output fields: `session_id`, short-lived session token, Razorpay public key, order ID, fixed amount, currency, expiry, and redacted email mode.

### `POST /demo/sessions/{id}/checkout-events`

- Require the session token and a unique `client_event_id`.
- Accept only the four approved event types.
- Reject unknown metadata and events for expired or mismatched sessions.
- Bound event ingestion per session in Redis and in the database.
- Make duplicates return success without repeating timers or case creation.

### `GET /recover/{signed_token}`

- Verify signature, expiry, session, merchant, order, amount, and currency.
- Re-read Razorpay order/payment state before returning Checkout bootstrap data.
- Reject changed amounts, paid orders, expired sessions, and invalid tokens.
- Reuse the original unpaid order; never create a Payment Link or silently charge.

### `GET /demo/sessions/{id}`

Return one sanitized projection suitable for two-second polling:

- Session and Checkout state
- Current case and deterministic diagnosis
- Optional Luna insight plus fallback status
- Gate verdict and recovery URL availability
- Razorpay, Luna, and Resend provider statuses
- Redacted timeline
- Operational metrics: cases detected, recovered cases, recovered amount, recovery rate, median recovery time, provider failures, and Luna cost

### Webhook endpoints

`POST /webhooks/razorpay` keeps raw-body HMAC verification, commit-before-queue behavior, deduplication, and asynchronous processing. Add normalization for `payment.failed`, `payment.captured`, and `order.paid`.

Add `POST /webhooks/resend` with the same durable-inbox pattern. Verify the provider signature against the raw body before JSON parsing or persistence and normalize sent, delivered, bounced, complained, clicked, and failed events.

## 6. Integration behavior

### Razorpay

- Fail `live_demo` startup unless the public key starts with `rzp_test_`.
- Create orders server-side with fixed amount and currency; the browser must not choose either value.
- Use webhook truth as authoritative. Browser `checkout_completed` is advisory until verified against Razorpay.
- On `payment.failed`, create or update one `PAYMENT_FAILURE` case using an attempt/order-stable dedupe key.
- On `payment.captured` or `order.paid`, close and attribute the matching open case regardless of event order or duplicate delivery.
- If both abandonment and failure evidence appear, retain only `PAYMENT_FAILURE` for that attempt and cancel any pending abandonment task.

### Browser telemetry

- Generate `client_event_id` in the browser and persist it before retrying a request.
- Use server receipt time for timer scheduling while retaining client occurrence time as evidence.
- A dismissal without a payment failure schedules a re-check at +30 seconds.
- A worker creates `CHECKOUT_ABANDON` only if the session is active, the order is unpaid, no failure case exists, and no later attempt/completion invalidated the dismissal.

### Luna

- Use the Responses API with model `gpt-5.6-luna`, reasoning effort `low`, structured JSON output, no tools, `store=false`, and a bounded output limit.
- Send only deterministic classification, payment method, amount band, and aggregate provider fields. Exclude email, name, phone, raw session/order/payment IDs, webhook bodies, and free-form customer input.
- Run the call after deterministic diagnosis. Luna may explain evidence and suggest the already-registered next step, but it cannot select actions, alter consent, approve a gate, or create customer-facing copy.
- Record request ID, prompt version, token usage, computed cost, latency, retries, and schema validity.
- Permit one retry for transport or invalid-schema failure within the task budget. On timeout, budget exhaustion, or final schema failure, record the error and show the deterministic explanation immediately.

### Resend

- Render only the registered recovery template; use the signed recovery URL as a declared variable.
- Send no more than one recovery email per case and five per allowlisted address per rolling day.
- Use the action idempotency key as the provider idempotency key.
- Track local daily/monthly usage. At 80%, raise a warning; before the configured free-tier ceiling, switch subsequent sends to `quota_blocked` previews.
- Non-allowlisted recipients always receive `preview_only` status and no provider call.
- Record webhook delivery status without exposing the address in APIs, logs, or timeline entries.

## 7. Action ladder and timing

Create live-only ladders; do not modify the simulation ladders.

| Case type | Step 0 | Step 1 | Stop condition |
|---|---|---|---|
| `PAYMENT_FAILURE` | Immediate signed in-app recovery link | Email after 30 demo seconds | Any verified successful payment for the original order |
| `CHECKOUT_ABANDON` | Immediate signed in-app recovery link | Email after 30 demo seconds | A later failure replaces abandonment, or the order is paid |

Every external email action passes through the current guardrail gate immediately before execution. Creating or displaying the recovery URL is not a debit; completing Checkout always requires customer authorization.

## 8. Configuration and startup gates

Document these settings in `.env.example` without values:

```text
LEAKPROOF_MODE=simulation|live_demo
LEAKPROOF_PUBLIC_BASE_URL=
LEAKPROOF_RECOVERY_TOKEN_SECRET=
LEAKPROOF_RAZORPAY_KEY_ID=
LEAKPROOF_RAZORPAY_KEY_SECRET=
LEAKPROOF_RAZORPAY_WEBHOOK_SECRET=
LEAKPROOF_OPENAI_API_KEY=
LEAKPROOF_OPENAI_MODEL=gpt-5.6-luna
LEAKPROOF_RESEND_API_KEY=
LEAKPROOF_RESEND_WEBHOOK_SECRET=
LEAKPROOF_RESEND_FROM_EMAIL=
LEAKPROOF_DEMO_EMAIL_ALLOWLIST=
LEAKPROOF_RESEND_DAILY_LIMIT=
LEAKPROOF_RESEND_MONTHLY_LIMIT=
```

`live_demo` readiness must fail when HTTPS base URL, test-mode Razorpay credentials, webhook secrets, recovery signing secret, or required provider configuration is missing. `simulation` must continue to start without external credentials.

## 9. File-level implementation map

| Area | Primary project locations | Change |
|---|---|---|
| Settings | `src/leakproof/config.py`, `.env.example` | Add live-demo/provider settings and startup validation |
| Database | `src/leakproof/models/db.py`, new Alembic migration | Add sessions, telemetry, delivery/insight persistence and indexes |
| Razorpay | `src/leakproof/sensors/webhooks.py`, `src/leakproof/sensors/normalizer.py`, new `providers/razorpay.py` | Orders/payment adapter, event normalization, success reconciliation |
| Demo API | `src/leakproof/api/app.py`, new `demo/` service module | Session, telemetry, recovery, and projection endpoints |
| Scheduling | `src/leakproof/celery_app.py`, `src/leakproof/actuators/executor.py` | Abandonment deadline and live adapter dispatch |
| Luna | `src/leakproof/diagnosis/tier2.py` or new `insights/` module | Responses transport, strict `CaseInsight`, safe fallback |
| Resend | new `actuators/resend.py`, webhook ingestion/normalization | Idempotent template send, quotas, delivery updates |
| Policy | `config/actions.yaml`, `config/ladders.yaml`, `config/templates.yaml` | Live-only actions and email template without changing simulator paths |
| Dashboard | `dashboard/app/`, `dashboard/lib/api.ts`, `dashboard/lib/types.ts` | Live Demo default, polling, badges, timeline, live metrics, Scenario Lab split |
| Tests | `tests/` plus webhook fixtures | Unit, contract, precedence, abuse, fallback, and end-to-end coverage |

## 10. Daily execution schedule

Each day ends with a demonstrable vertical checkpoint. Incomplete critical-path work is resolved first the next morning before new scope starts.

### 29 August — contracts, schema, and safe configuration

**Build**

- Freeze request/response schemas, case dedupe rules, state transitions, and provider protocols.
- Add `live_demo` settings and startup gates while preserving simulation defaults.
- Add the migration for demo sessions, checkout events, and required provider state.
- Add fake adapters and test fixtures so subsequent work does not depend on provider availability.
- Add skeleton endpoints returning typed errors rather than placeholders.

**Definition of done**

- Migration upgrades and downgrades cleanly on a disposable database.
- Simulation mode boots without external secrets.
- Live-demo mode refuses non-test Razorpay keys and missing signing/webhook secrets.
- Contract tests lock all four new public routes and both webhook envelopes.

### 30 August — Razorpay order creation and Checkout telemetry

**Build**

- Implement Razorpay authentication and `POST /v1/orders` adapter.
- Implement `POST /demo/sessions` with fixed amount, expiry, allowlist mode, and IP throttling.
- Implement the Checkout UI bootstrap and the four idempotent browser telemetry events.
- Implement dismissal scheduling and the 30-second abandonment state re-check.

**Definition of done**

- A browser session creates one real Razorpay test order and opens Checkout.
- Duplicate telemetry does not create duplicate rows or timers.
- Dismissal creates one `CHECKOUT_ABANDON` case only after 30 seconds and only while unpaid.

### 31 August — Razorpay webhooks, recovery URL, and successful close

**Build**

- Normalize `payment.failed`, `payment.captured`, and `order.paid` from the verified inbox.
- Implement failure-over-abandonment precedence and out-of-order reconciliation.
- Implement signed 30-minute recovery tokens bound to session, merchant, order, amount, and currency.
- Implement recovery bootstrap against the original unpaid order.
- Cancel pending recovery actions when payment succeeds.

**Definition of done**

- Invalid signatures are rejected and duplicate webhooks have one business effect.
- A failed payment creates one `PAYMENT_FAILURE` case, not a second abandonment case.
- Either success webhook closes the same case even when events arrive out of order.
- Expired or modified recovery tokens fail closed.

### 1 September — Luna case insights and deterministic fallback

**Build**

- Implement the OpenAI Responses adapter and strict `CaseInsight` schema.
- Add a PII-minimizing input builder and prompt version.
- Persist request metadata, usage, latency, cost, retries, and schema status.
- Trigger insights asynchronously after Tier 1 diagnosis.
- Add deterministic fallback for timeout, quota, invalid output, and budget stop.

**Definition of done**

- Valid structured output appears in the session projection and timeline.
- PII exclusion tests prove that prohibited fields never enter the provider payload.
- Recovery remains available when Luna is slow, unavailable, over budget, or invalid.

### 2 September — Resend delivery, webhooks, and quota protection

**Build**

- Implement the registered email template and Resend adapter.
- Schedule email 30 demo seconds after the in-app recovery link.
- Enforce allowlist, one-per-case, per-address daily limit, provider idempotency, and local quota budget.
- Add signed Resend webhook ingestion and delivery-state normalization.
- Redact recipients in API output, timelines, and structured logs.

**Definition of done**

- An allowlisted address receives at most one email for the case.
- Non-allowlisted, rate-limited, and quota-blocked requests produce safe previews with no provider call.
- Duplicate and out-of-order Resend events converge on the correct visible delivery state.

### 3 September — live dashboard, integrated tests, and hardening

**Build**

- Make Live Demo the default dashboard and move the existing synthetic experience under Scenario Lab.
- Poll the session projection every two seconds only while the session is active.
- Add source badges for Browser Telemetry, Razorpay Webhook, Luna, and Resend.
- Show the live case state, diagnosis, insight/fallback, gate, recovery action, provider receipt, recovered amount, and latency.
- Run unit, contract, abuse-control, redaction, duplicate, out-of-order, and end-to-end suites.
- Validate deployment HTTPS and provider webhook configuration.

**Definition of done**

- Live metrics contain no simulator or treatment/holdout data.
- Scenario Lab has an unmistakable synthetic label and unchanged regression behavior.
- The critical test suite passes from a clean database and the UI uses real API responses.

### 4 September — acceptance, rehearsal, and implementation freeze

**Build**

- Deploy the release candidate and register the stable HTTPS Razorpay and Resend webhook URLs.
- Execute both hero paths: Checkout dismissal and Razorpay payment failure.
- Exercise email preview, allowlisted delivery, Luna fallback, duplicate webhooks, expired token, and later successful payment.
- Export one sanitized audit timeline and final operational metrics.
- Align README/setup instructions and record known exceptions.
- Freeze implementation by noon; reserve the remainder for fixes, repeatability, and rehearsal.

**Definition of done**

- A reviewer completes the full acceptance flow twice from fresh sessions without manual data changes.
- All blocking and high-severity defects are closed; lower-severity exceptions are documented.
- Setup, demo script, rollback steps, and credential ownership are documented.

## 11. Test matrix

| Area | Required cases |
|---|---|
| Razorpay adapter | success, timeout, authentication failure, 429/5xx retry policy, malformed response, request ID capture |
| Razorpay webhooks | valid/invalid signature, duplicate event, missing ID fingerprint, failure then success, success then late failure, captured/order-paid duplication |
| Telemetry | duplicate client ID, event flood, dismissal without attempt, dismissal then failure, dismissal then completion, stale/expired session |
| Recovery token | valid, expired, altered order, altered amount, altered merchant, already-paid order |
| Luna | valid schema, extra fields, invalid confidence, timeout, budget exhausted, one retry, PII exclusion, deterministic fallback |
| Resend | allowlisted, preview-only, one-per-case, five-per-day boundary, quota warning/block, provider failure, duplicate/out-of-order delivery events |
| Isolation and abuse | session-token mismatch, cross-session access, 10-per-IP boundary, log redaction, bounded payload sizes |
| End to end | dismissal recovery and failed-payment recovery, each ending in the original order paid and the same case closed |

The existing simulator regression suite remains mandatory.

## 12. Observability and operating checks

Use structured logs with `session_id`, `case_id`, action ID, provider, provider request ID, and safe error class. Never log recipient, provider secrets, raw identifiers sent to Luna, signed recovery tokens, or full webhook bodies.

Track at minimum:

- Session creation success/failure and IP throttles
- Webhook signature failures, duplicate rate, inbox lag, and processing retries
- Abandonment timers scheduled, cancelled, and materialized
- Provider latency/error rate for Razorpay, Luna, and Resend
- Luna tokens and cost per case
- Resend sends, previews, quota warnings, bounces, complaints, and failures
- Recovery-token validation failures by reason
- Detected cases, recovered cases, amount recovered, recovery rate, and median recovery time

Alerts for the demo window: webhook processing lag over 30 seconds, provider error burst, Resend quota at 80%, any non-test Razorpay key, and any PII-redaction test failure.

## 13. Release and rollback

### Release order

1. Apply the forward migration.
2. Deploy API/workers with `LEAKPROOF_MODE=simulation`; confirm readiness.
3. Configure secrets and HTTPS webhook endpoints.
4. Switch only the demo deployment to `live_demo`.
5. Run a test session, then expose Live Demo navigation.

### Kill switches

- Disable outbound email while retaining previews.
- Disable Luna calls while retaining deterministic diagnosis.
- Disable new demo sessions while continuing webhook reconciliation for existing sessions.
- Switch the dashboard back to Scenario Lab without deleting live audit data.

Do not roll back by deleting events or cases. If a provider is unhealthy, stop new calls, preserve the inbox, and replay safely after recovery.

## 14. Final acceptance gate

- [ ] Live-demo startup accepts only Razorpay test credentials.
- [ ] A new session creates a real test order with server-fixed amount and currency.
- [ ] Checkout dismissal creates one abandonment case after the verified 30-second delay.
- [ ] `payment.failed` creates one payment-failure case and takes precedence over abandonment.
- [ ] Luna returns a stored structured insight without receiving prohibited PII.
- [ ] Luna failure never blocks diagnosis, recovery URL generation, or payment.
- [ ] Recovery tokens expire after 30 minutes and reject tampering.
- [ ] The original unpaid order is reused for the customer-authorized retry.
- [ ] Email is sent only to an allowlisted recipient and no more than once per case.
- [ ] Resend quotas switch safely to previews before the configured ceiling.
- [ ] Razorpay and Resend webhook signatures, duplicates, and out-of-order events are handled safely.
- [ ] `payment.captured` or `order.paid` closes the same case and cancels later actions.
- [ ] Live timeline and metrics update without manual database changes.
- [ ] Live and synthetic metrics are visibly and technically separated.
- [ ] The full simulator test suite still passes.
- [ ] The complete reviewer flow succeeds twice on 4 September.

## 15. Critical path

```text
Session schema/config
  -> Razorpay order creation
  -> Checkout telemetry and verified webhooks
  -> case precedence and recovery token
  -> original-order successful retry
  -> Luna and Resend enrichment
  -> dashboard projection
  -> end-to-end acceptance
```

Razorpay order creation, webhook reconciliation, and successful original-order retry are the critical path. Luna and Resend must degrade safely and may not prevent the core payment loop from shipping.

## 16. External implementation references

- [OpenAI GPT-5.6 Luna model reference](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
- [OpenAI Responses API create reference](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)

Provider account limits and webhook configuration should be rechecked in the Razorpay and Resend dashboards immediately before the 4 September release rehearsal.
