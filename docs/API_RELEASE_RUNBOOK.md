# September 4 API release and rehearsal runbook

This runbook covers the repository-controlled part of the 4 September release. The Razorpay path is
sandbox-only: do not activate Live Mode, complete KYC, add a settlement bank account, or enter real
payment details. Application secrets must remain in the deployment secret store and must never be
pasted into acceptance artifacts.

## Ownership

| Responsibility | Owner role | Evidence |
|---|---|---|
| Deployment, HTTPS URL, database migration, and rollback | Application operator | Healthy deployment revision and migration log |
| Razorpay Test Mode keys | Razorpay test-account administrator | Key ID begins with `rzp_test_`; no Live Mode activation evidence is required |
| Resend API key, verified sender, and webhook registration | Resend account administrator | Verified sender and signed webhook endpoint |
| OpenAI project key and spend boundary | OpenAI project administrator | Project key is active and the application budget is configured |
| Two hero-path runs and artifact review | Release reviewer | Two sanitized `2026-09-04` artifacts with `passed: true` |

## Preflight and release

For local baseline verification with an existing stack or audit volume, run
`make release-gate-isolated`. It runs the existing automated gate in a temporary source copy,
omits local `.env` files, uses simulation providers and a separate Compose project with loopback
ports, and disables model calls and outbound email in its services. Logs and synthetic results
are saved under `artifacts/baseline/`; the test volume and source copy are retained, and only the
isolated containers are stopped afterward. Existing provider-rehearsal artifacts are preserved
and must be validated separately with `make release-evidence`. This does not perform a new
Razorpay or recipient-delivery rehearsal.

1. Copy `.env.example` into the deployment secret workflow and set every live-demo value. Generate
   `LEAKPROOF_OPERATOR_API_TOKEN` from at least 32 random bytes, scope
   `LEAKPROOF_OPERATOR_MERCHANT_IDS` to the demo merchant, and keep
   `LEAKPROOF_OPERATOR_UI_ENABLED=false` on the public dashboard. Keep
   `LEAKPROOF_MODE=simulation` for the first boot.
2. Confirm `LEAKPROOF_RAZORPAY_KEY_ID` begins with `rzp_test_`. The application rejects live keys.
   Test Mode is the complete scope of this release; stop if any flow asks for KYC or bank details.
3. Deploy the release candidate, apply `alembic upgrade head`, and check `GET /health/live` and
   `GET /health/ready`.
4. The core Razorpay rehearsal needs no Dashboard webhook registration. Checkout posts its signed
   success fields to `/demo/sessions/{session_id}/payments/verify`; the server uses its own order ID
   for HMAC verification and then requires the Razorpay Payments API to report `captured` with the
   exact amount and currency. A browser failure only schedules an order-payment API recheck; browser
   data never decides payment truth. Optionally register these stable HTTPS endpoints when the
   sandbox Dashboard makes webhook settings available:

   - Razorpay: `<LEAKPROOF_PUBLIC_BASE_URL>/webhooks/razorpay` for `payment.failed`,
     `payment.captured`, and `order.paid`.
   - Resend: `<LEAKPROOF_PUBLIC_BASE_URL>/webhooks/resend` for sent, delivered, bounced,
     complained, clicked, and failed events.

5. Switch only the demo deployment to `LEAKPROOF_MODE=live_demo` and restart the API, worker, and
   scheduler. A startup failure means the enabled provider configuration is incomplete; do not
   bypass it.
6. Confirm anonymous/invalid bearer requests to `/cases`, `/scoreboard/latest`, `/evals/latest`,
   `/costs`, and `/suppressions` return `401`, and a valid credential cannot read another
   merchant's case. Production identity must replace this buildathon token boundary.
7. Run `make release-gate-automated` against the release commit before opening public navigation.
   It builds both images, migrates a disposable database from zero and reuses it once, runs the
   complete test/build/security/evaluation suite, verifies the foundation twice, proves batch
   replay idempotency, scans browser assets for credential values, and captures the synthetic AI
   incident plus model-disabled fallback evidence.

## Rehearsal

Complete each hero path from a fresh browser session. Do not edit the database or manually close a
case.

1. Checkout dismissal: create a session, open Checkout, dismiss it, wait for the configured
   seven-second public-demo delay and bounded provider-state recheck, use the recovery route, and
   complete the original order.
2. Payment failure: create a new session, cause a Razorpay test payment failure, verify only one
   `PAYMENT_FAILURE` case exists, use the recovery route, and complete the original order.

For the two runs together, verify allowlisted delivery and preview-only email behavior. During a
separate test run, disable Luna with `LEAKPROOF_LUNA_ENABLED=false` and confirm deterministic
fallback does not block recovery. If optional webhooks are configured, replay one; otherwise rely
on the automated webhook replay gate. Also test an expired recovery link.

After each successful session, keep its session token out of shell history by exporting it through
a hidden or protected environment workflow, then capture the evidence:

```bash
read -rs LEAKPROOF_REHEARSAL_SESSION_TOKEN
export LEAKPROOF_REHEARSAL_SESSION_TOKEN
uv run python scripts/capture_api_acceptance.py \
  --base-url https://demo.example.com \
  --session-id '<session id>' \
  --output artifacts/api-acceptance/hero-path-1.json
unset LEAKPROOF_REHEARSAL_SESSION_TOKEN
```

Repeat with `hero-path-2.json`. The command polls for up to three minutes, exits nonzero while any
blocking check remains open, and saves only the sanitized timeline, operational metrics, safe
provider statuses, and acceptance checks. It omits session/order/action/provider identifiers,
browser attempt IDs, recipients, recovery URLs, and tokens.

Validate both files as live, sanitized, complementary hero-path evidence:

```bash
uv run python scripts/validate_acceptance_artifacts.py \
  --directory artifacts/api-acceptance \
  --require-live \
  --require-both-hero-paths
```

The validator requires explicit proof of original-order reuse, provider-verified payment,
same-case closure, pending-contact cancellation, exact current-session recovered amount, audit
replay parity, and no blocking Razorpay failure. It rejects identifier-shaped fields, recipients,
tokens, signed recovery URLs, and provider entity IDs.
The API also redacts the signed capability segment from Uvicorn recovery-route access logs; verify
runtime logs show `/recover/[REDACTED]` rather than a token-bearing URL.

## Meaningful AI incident rehearsal

Capture the labelled synthetic incident replay through the production observation aggregation,
proposal, validation, and scoped-consequence path:

```bash
make ai-incident-evidence
```

The artifact records the observed `47 / 52` current failures and `4 / 100` baseline failures,
allowlisted aggregate scope, proposal/validation audit events, 47 matching affected cases, zero
unrelated affected cases, model cost/latency/schema status, and a separate model-disabled run with
zero suppressions plus audited `NO_ACTION`. It contains no customer or entity identifiers and is
labelled `SIMULATED_END_TO_END`; it is not provider-verified revenue evidence.

## 90-second primary demo script

| Time | Show | Say |
|---:|---|---|
| 0–20s | Create a ₹500 session, dismiss Checkout, then use **Continue recovery** | “Browser dismissal is only a signal. The server rechecks Razorpay before opening one case.” |
| 20–40s | Complete the original order and show the same case close | “Only a server-verified signature plus captured Razorpay status—or a signed webhook—closes the case, cancels pending contact, and updates this session’s verified amount.” |
| 40–62s | Open the HDFC/netbanking incident replay | “Observed denominators qualify the slice; the model proposes a narrow intervention, but deterministic validation controls authority.” |
| 62–76s | Show 47 matching cases suppressed and the unaffected control | “Scope is enforced. Unrelated payment cases continue.” |
| 76–86s | Show Scenario Lab assumptions, interval, and exceptions | “These are simulated estimates with declared uncertainty, separate from verified live money.” |
| 86–90s | End on the architecture boundary | “AI resolves ambiguity; policy controls action.” |

If a backup recording is required for the submission, record this once after the two successful
rehearsals, with browser developer tools closed and all identifiers cropped or blurred. Review the
recording once with audio muted and once visually hidden; store the approved file outside Git as
`artifacts/demo/leakproof-90s-backup.mp4`. Do not record Checkout-entered contact or payment
details, API keys, session tokens, order IDs, signed URLs, or provider dashboards containing
credentials.

Run `make demo-recording-check` after the two manual reviews. The structural check requires an MP4
with a video track, a 75–105 second duration, and a non-placeholder file size. It cannot inspect the
recording for sensitive content, so the audio-muted and visually-hidden reviews remain mandatory.

## Release gate

After both artifacts exist, run the single final command:

```bash
make release-gate
```

The release passes when both artifacts have `passed: true`, the reviewer completes both flows
without data changes, the AI incident and model-disabled artifacts pass, the full clean/reused gate
is green, and no blocking or high-severity defect remains.
Advisory provider failures are acceptable only when the deterministic fallback and core recovery
path completed and the exception is recorded below.

## Kill switches and rollback

Use the smallest switch that contains the incident. These changes preserve provider reconciliation
and existing audit data.

- Stop new sessions: set `LEAKPROOF_DEMO_SESSIONS_ENABLED=false`.
- Stop Luna calls: set `LEAKPROOF_LUNA_ENABLED=false`; cases receive deterministic fallback.
- Stop outbound email: set `LEAKPROOF_OUTBOUND_EMAIL_ENABLED=false`; actions become safe previews.
- Remove Live Demo from public navigation or switch the deployment to `simulation` if the complete
  live surface must be withdrawn.

Restart the affected API/worker processes after changing configuration. Do not delete provider
events, sessions, cases, or audit rows. If optional webhooks are configured, leave their endpoints
available so existing events can reconcile. A code rollback must use a release compatible with the
migrated schema; database downgrade is not part of the incident path.

## Known lower-severity exceptions

- Successful sandbox Checkout cannot be completed by an offline test suite. It requires a human to
  use Razorpay's mock Test Mode payment UI; it never requires real bank or card data.
- Razorpay sends no payment event for a pure Checkout dismissal, so abandonment depends on bounded
  first-party browser telemetry plus the server-side seven-second public-demo state recheck. The
  delay is separately configurable and must be longer in a production deployment.
- Resend delivery is intentionally restricted to the configured allowlist. Every other address is
  preview-only and is not evidence of a provider delivery failure.
- The durable inbox is authoritative. If immediate Celery enqueue fails, the scheduler can add up
  to 60 seconds of recovery lag before redispatching the stored webhook.
- A Luna or Resend outage can make the no-provider-failures acceptance check advisory-fail. It does
  not waive any blocking diagnosis, recovery, original-order payment, or same-case closure check.

## Declared limitations

- Razorpay evidence is test-mode only; it proves integration and reconciliation, not production
  payment volume or realized lift.
- Scenario Lab treatment effects and economics are simulated estimates driven by declared
  assumptions, contribution margin, excluded costs, and uncertainty ranges.
- The bearer operator credential is buildathon containment, not production identity. Production
  requires merchant-scoped OAuth/RBAC, rotation, and an authenticated operator surface.
- Invoice, subscription, mandate, voice, Resend recipient delivery, and other provider integrations
  remain simulated, preview-only, or architecture-ready as labelled in the capability matrix.

## Track A — current checkout-abandonment acceptance

Code and automated contract checks are complete. **Fresh provider rehearsal is pending.**
The existing September 1 provider exports remain historical evidence. Do not relabel the new
synthetic contract captures as real Razorpay payments.

### Repeatable automated checks

`make track-a-contract` extends the existing August 30/31 and September 2/4 backend suites and
artifact validator. It writes two sanitized **SIMULATED_END_TO_END** exports to
`artifacts/track-a/contract/`, using the existing fake providers. No account credentials or network
payments are used. The tests also cover stale/equal-time events, broker retry, provider failure,
expiry, failure precedence, original-order reuse, pending-contact cancellation, and audit replay.

For browser contract checks, start a local dashboard on an unused port with its backend pointed
at a closed loopback port, then run the capture in a second terminal:

```bash
API_BASE_URL=http://127.0.0.1:9 npm --prefix dashboard run dev -- --hostname 127.0.0.1 --port 3100
```

```bash
uv run playwright install chromium
make track-a-contract track-a-browser
```

The browser check intercepts API and Checkout SDK responses, blocks external browser requests,
uses the real application components, and writes watermarked desktop/mobile screenshots and
`summary.json` to `artifacts/track-a/browser/`. It tests queue replay after refresh, duplicate close
callbacks, waiting/retry/pending displays, original-order checks on every recovery click, same-case
completion, export, failure precedence, stale recovery and expiry. These are **browser contract
fixtures**, not provider-payment evidence. `TRACK_A_BROWSER_URL` can select another local port.
The separate PostgreSQL regression reuses fresh/upgraded disposable database fixtures:

```bash
LEAKPROOF_TEST_POSTGRES_ADMIN_URL=postgresql+psycopg://leakproof:leakproof@localhost:55432/postgres \
  uv run pytest tests/test_multi_resource_migrations.py -k track_a
```

### Exact human Razorpay test-mode rehearsal

1. Load this working tree into the local demo with `make up` when ready to update the existing
   application. This builds the API/dashboard, applies the forward migrations, and starts the
   worker and Beat. Use the already configured `live_demo` environment with `rzp_test_` credentials;
   the isolated browser-check server on port 3100 is not the provider demo. This implementation
   step did not update or restart the existing deployment.
2. Open `http://localhost:3000/demo`. If an older session is active, use **Live dashboard → Start a
   new demo**, which returns to the scenario chooser before creating another order. A fresh private browser
   window also starts without an older session selection.
3. Select **Checkout abandonment**, leave recovery email blank for preview-only contact, and click
   **Start checkout abandonment**. Note the order suffix; the amount must remain ₹500.
4. Close the Razorpay modal without attempting payment; confirm the modal's close prompt. Observe
   **Dismissal recorded · recheck in …s**, followed by **Waiting for provider recheck** if the
   provider read has not finished. The default wait is 7 seconds; scheduled rescue runs every 15
   seconds. Refresh once: the same session/order and persisted wait must return. A provider error
   must show **Provider recheck delayed · retry scheduled**, not a confirmed abandonment.
5. Wait for **Abandonment confirmed · original order unpaid**, then click **Continue recovery**.
   Check that the order suffix and ₹500 amount match. Click **Continue original order**; this
   performs a fresh provider check before opening Checkout.
6. In Razorpay Test Mode, choose **Netbanking**, select any available bank, continue to the mock
   bank page, and choose **Success**. Alternatively use the currently documented domestic Visa
   test card **4100 2800 0000 1007**, a future expiry such as **12/30**, and any test CVV such as
   **123**, then choose **Success** on the mock page. Use only test details. These steps were checked
   against [Razorpay's Standard Checkout test-integration instructions](https://razorpay.com/docs/payments/payment-gateway/web-integration/standard/integration-steps/)
   on 2026-09-03. Do not treat the mock-page click as application payment evidence: the server must
   still verify the signed result and captured amount/currency against the original order.
7. On the Live dashboard, verify **Payment verified · recovery complete**, the same case closed,
   exactly ₹500 recovered for this session, and no pending email. Contact may be previewed already
   or cancelled if recovery finished before dispatch. No recipient-delivery claim follows from
   preview/cancellation. If capture is still pending, retain that state; do not manually close a case.
8. Click **Download acceptance evidence** before session expiry. Save under a new filename such as
   `artifacts/api-acceptance/track-a-current-checkout-abandon.json`, preserving historical files.
   Require `passed: true`, `case.leak_type: CHECKOUT_ABANDON`,
   `session.scenario_type: CHECKOUT_ABANDON`, and
   `data_provenance: LIVE_TELEMETRY_PROVIDER_RECONCILED`. The new checks require browser dismissal,
   a successful unpaid-order recheck, and original-order recovery bootstrap. Payment failure takes
   precedence if a failed attempt occurs: keep that evidence and start a fresh abandonment run.
9. Validate the new file without mixing it with the legacy two-file release gate:

   ```bash
   uv run python scripts/validate_acceptance_artifacts.py \
     artifacts/api-acceptance/track-a-current-checkout-abandon.json --require-live
   ```

The CLI capture remains available; add `--scenario-type CHECKOUT_ABANDON` to the existing protected
session-token command above. The browser download requires no copying of session tokens. Incomplete
exports retain failed checks. A transient provider failure followed by a successful retry is advisory;
an unresolved latest failure remains blocking. If the session expires, recovery and telemetry stop,
pending email is cancelled when dispatched, and a new rehearsal must be started.

**Provider status: PENDING.** No human Razorpay payment, current live acceptance export, or new
recipient-delivery proof was produced by this implementation step.

## Track B — invoice recovery acceptance

Track B is implemented and contract/browser tested. A real provider rehearsal is still pending.
The browser fixtures and `artifacts/track-b/contract/` exports are simulated evidence. They do not
establish a Razorpay invoice, webhook registration, email delivery, or payment.

1. Configure the existing live-demo deployment with a Razorpay **test** key and an existing
   Test Mode customer ID in `LEAKPROOF_DEMO_INVOICE_CUSTOMER_ID`. The recipient email is independent
   of this provider customer; an email address never selects or establishes invoice ownership.
   The API creates a non-GST draft with provider SMS/email disabled and partial payments enabled,
   then issues that same invoice. GST invoices remain Dashboard setup. No new provider customer
   or recipient is created automatically.
2. Keep the existing migration head `0011_multi_resource`. Deploy the updated API, worker, Beat,
   and dashboard together. No new migration is needed. Beat uses the existing
   `leakproof.poll_invoice_aging` task, now every `LEAKPROOF_INVOICE_RECONCILE_SECONDS` (default 30).
   Reconciliation reads invoices registered to this configured merchant; it does not import the
   account's unregistered invoices or infer a due date for them.
3. Register/verify the existing HTTPS webhook endpoint in Razorpay Test Mode for
   `invoice.partially_paid`, `invoice.paid`, and `invoice.expired`, retaining payment/order events.
   Use the matching secret and merchant configuration. Confirm signed delivery reaches the inbox
   and the worker processes it. A populated secret alone does not establish event delivery.
4. Set `LEAKPROOF_DEMO_INVOICE_DUE_SECONDS` (default 60) for the rehearsal. The persisted policy is
   **setup time plus this interval**. This is application business aging, independent of provider
   status and `expire_by`. The UI displays both dates. For a separate expiry rehearsal set
   `LEAKPROOF_DEMO_INVOICE_EXPIRY_MINUTES=16` and `LEAKPROOF_DEMO_SESSION_TTL_MINUTES=60`; the
   default 60-minute provider expiry exceeds the default 30-minute session lifetime. Respect
   Razorpay's minimum future expiry and allow provider time to pass.
5. Open `/demo`, select **Overdue invoice**, and create the test invoice. An optional recipient
   receives email only if allowlisted, enabled, and permitted by the existing contact/budget gate.
   Otherwise the action remains a preview. Wait for the business due date and Beat reconciliation;
   one `INVOICE_OVERDUE` case should appear with the original detected balance and current outstanding.
6. Choose **Continue recovery**, then **Continue original invoice**. Each click re-fetches provider
   truth. The approved URL must be the original `https://rzp.io/i/...` invoice page. As a human,
   pay part of the invoice, return to the dashboard, and wait for reconciliation. The case stays
   open, current outstanding decreases, and original detected balance stays fixed. Pay the remaining
   balance on the same invoice. Full invoice settlement backed by captured payment IDs closes the
   same case; overlapping notifications cannot add the invoice total a second time.
7. With a separate unpaid invoice, wait for expiry or cancel it manually in the Dashboard. Do not
   cancel a partially paid invoice. Expect **merchant review**, no payment CTA, and no recovery
   credit for expiry/cancellation. Recovery never extends expiry, issues another invoice, creates
   another order, or creates a Payment Link. A stale link is rechecked before any hosted navigation.
   Unknown/contradictory balances and provider outages hold the CTA and retry; they do not prove payment.
8. Download **acceptance evidence** from the authenticated dashboard. Partial/full settlement and
   non-payable fallback use separate acceptance checks. Incomplete exports retain failed checks.
   Alternatively use the existing capture CLI, supplying the token through its environment variable:

   ```sh
   uv run python scripts/capture_api_acceptance.py \
     --session-id "$TRACK_B_SESSION_ID" --scenario-type INVOICE_OVERDUE \
     --output artifacts/api-acceptance/invoice-partial-full-new.json
   uv run python scripts/validate_acceptance_artifacts.py \
     artifacts/api-acceptance/invoice-partial-full-new.json --require-live
   ```

   Use a new filename for the separate expired/cancelled capture. Do not overwrite historical
   provider artifacts or change simulated provenance labels. Captures exclude tokens, recipients,
   hosted URLs, and provider invoice/payment/order/customer IDs.

If create fails, inspect the provider Dashboard/ledger before starting another setup: a timed-out
write may have reached Razorpay. If issue fails, the app retains the exact draft ID and reports
`ACTION_REQUIRED`; the job only re-fetches it. A merchant may inspect/issue that draft in the
Dashboard. Recovery never retries setup writes. An issuance timeout that actually succeeded can
be discovered by reconciliation; its original failed-call evidence remains visible.

Source checks on 2026-09-03: Razorpay's [invoice creation contract](https://razorpay.com/docs/api/payments/invoices/create-with-customer-id/)
provides notification controls, partial payments and minimum future expiry; its
[invoice states](https://razorpay.com/docs/payments/invoices/states/) prohibit customer payments
on expired/cancelled invoices. These are documented capabilities, not account acceptance evidence.
