# September 4 API release and rehearsal runbook

This runbook covers the repository-controlled part of the 4 September release. Provider account
changes must be completed by an authorized owner in the Razorpay and Resend dashboards; application
secrets must remain in the deployment secret store and must never be pasted into acceptance
artifacts.

## Ownership

| Responsibility | Owner role | Evidence |
|---|---|---|
| Deployment, HTTPS URL, database migration, and rollback | Application operator | Healthy deployment revision and migration log |
| Razorpay test keys and webhook registration | Razorpay test-account administrator | Dashboard shows the release HTTPS URL and required events |
| Resend API key, verified sender, and webhook registration | Resend account administrator | Verified sender and signed webhook endpoint |
| OpenAI project key and spend boundary | OpenAI project administrator | Project key is active and the application budget is configured |
| Two hero-path runs and artifact review | Release reviewer | Two sanitized `2026-09-04` artifacts with `passed: true` |

## Preflight and release

1. Copy `.env.example` into the deployment secret workflow and set every live-demo value. Keep
   `LEAKPROOF_MODE=simulation` for the first boot.
2. Confirm `LEAKPROOF_RAZORPAY_KEY_ID` begins with `rzp_test_`. The application rejects live keys.
3. Deploy the release candidate, apply `alembic upgrade head`, and check `GET /health/live` and
   `GET /health/ready`.
4. Register these stable HTTPS endpoints:

   - Razorpay: `<LEAKPROOF_PUBLIC_BASE_URL>/webhooks/razorpay` for `payment.failed`,
     `payment.captured`, and `order.paid`.
   - Resend: `<LEAKPROOF_PUBLIC_BASE_URL>/webhooks/resend` for sent, delivered, bounced,
     complained, clicked, and failed events.

5. Switch only the demo deployment to `LEAKPROOF_MODE=live_demo` and restart the API, worker, and
   scheduler. A startup failure means the enabled provider configuration is incomplete; do not
   bypass it.
6. Run `make test-api-september-4` against the release commit before opening public navigation.

## Rehearsal

Complete each hero path from a fresh browser session. Do not edit the database or manually close a
case.

1. Checkout dismissal: create a session, open Checkout, dismiss it, wait for the verified
   30-second abandonment, use the recovery route, and complete the original order.
2. Payment failure: create a new session, cause a Razorpay test payment failure, verify only one
   `PAYMENT_FAILURE` case exists, use the recovery route, and complete the original order.

For the two runs together, verify allowlisted delivery and preview-only email behavior. During a
separate test run, disable Luna with `LEAKPROOF_LUNA_ENABLED=false` and confirm deterministic
fallback does not block recovery. Replay one provider webhook, and test an expired recovery link.

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

## Release gate

The release passes when both artifacts have `passed: true`, the reviewer completes both flows
without data changes, the full suite is green, and no blocking or high-severity defect remains.
Advisory provider failures are acceptable only when the deterministic fallback and core recovery
path completed and the exception is recorded below.

## Kill switches and rollback

Use the smallest switch that contains the incident. These changes preserve webhook reconciliation
and existing audit data.

- Stop new sessions: set `LEAKPROOF_DEMO_SESSIONS_ENABLED=false`.
- Stop Luna calls: set `LEAKPROOF_LUNA_ENABLED=false`; cases receive deterministic fallback.
- Stop outbound email: set `LEAKPROOF_OUTBOUND_EMAIL_ENABLED=false`; actions become safe previews.
- Remove Live Demo from public navigation or switch the deployment to `simulation` if the complete
  live surface must be withdrawn.

Restart the affected API/worker processes after changing configuration. Do not delete webhooks,
events, sessions, cases, or audit rows. Leave the verified webhook endpoints available so existing
payments can reconcile. A code rollback must use a release compatible with the migrated schema;
database downgrade is not part of the incident path.

## Known lower-severity exceptions

- Provider dashboard registration and successful customer-authorized Checkout cannot be completed
  by an offline test suite. They require account-owner action during the deployment rehearsal.
- Razorpay sends no payment event for a pure Checkout dismissal, so abandonment depends on bounded
  first-party browser telemetry plus the server-side 30-second state recheck.
- Resend delivery is intentionally restricted to the configured allowlist. Every other address is
  preview-only and is not evidence of a provider delivery failure.
- The durable inbox is authoritative. If immediate Celery enqueue fails, the scheduler can add up
  to 60 seconds of recovery lag before redispatching the stored webhook.
- A Luna or Resend outage can make the no-provider-failures acceptance check advisory-fail. It does
  not waive any blocking diagnosis, recovery, original-order payment, or same-case closure check.
