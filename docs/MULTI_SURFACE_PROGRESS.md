# Multi-surface recovery progress

## 2026-09-03 — Baseline preparation

Scope: inspect and stabilize the existing payment-failure and checkout-abandonment release before
starting the multi-surface milestones. No new recovery surface, provider resource, debit, outreach,
commit, push, deployment, or migration was introduced.

**Code status: baseline ready for milestone 0.** Two baseline defects fixed; the complete isolated
automated gate passed on 2026-09-03, with **248 tests and 88.07% coverage**. No new surface started.
**Provider status:** two historical Razorpay test-mode acceptance exports already exist and pass
the current validator. No new provider rehearsal was performed in this step. Both historical
exports use preview-only email, so they do not prove recipient delivery.

### Plans reconciled with implementation

The current sources of scope are the README, `API_INTEGRATION_PLAN.md`,
`API_INTEGRATION_BLUEPRINT.md`, `imporvements.md`, `docs/API_RELEASE_RUNBOOK.md`, and
`MULTI_SURFACE_RECOVERY_IMPLEMENTATION_PLAN.md`; the original blueprint/build specifications
describe the earlier simulator foundation. Existing unchecked plan items were checked against code
and evidence rather than treated as automatically unfinished implementation.

The earlier plans mention 30-second delays. Current settings and the release runbook use a
configurable **7-second demo delay**; the durable abandonment dispatcher runs every 15 seconds.
Those existing semantics were retained.

| Area | Actual implementation and reuse | Evidence / remaining boundary |
|---|---|---|
| Case and audit spine | Existing `RecoveryCase`, append-only events, actions, replay, attribution, and all five `LeakType` values reused | PostgreSQL enforcement is checked by the foundation gate; five enum values do not establish five live surfaces |
| Payment failure | Existing order/payment adapter, signed inbox, API failure detection, Checkout HMAC plus captured-payment verification, original-order tokens, and same-case closure reused | Historical payment artifact exists; new API precedence regression extends this existing path |
| Checkout abandonment | Existing persisted telemetry, delayed API recheck, Beat rescue, dedupe, original-order recovery, and failure precedence reused | Historical abandonment artifact exists; explicit scenario selection/countdown and the new provenance contract remain milestone work |
| Diagnosis, Luna, and policy | Existing Tier 1 authority, diagnosis refresh, bounded insight/fallback, provider-call ledger, and gates reused | Automated fallback is covered; historical artifacts record successful insights |
| Resend | Existing registered email action, encrypted allowlisted recipient, quotas, idempotency, webhook reconciliation, previews, and cancellation reused | Delivery code and contract tests are complete; both available provider rehearsals are preview-only |
| Dashboard | Existing Checkout component, Live Demo projection/timeline, recovery route, and separate Scenario Lab reused | No chooser, new CTA, or new live type added |
| Subscription normalization | `subscription.pending` / `subscription.halted` already normalize into initial generic signals | No live subscription adapter, session/cycle correlation, authorization recovery, or acceptance path |
| Invoice and mandate | Existing deterministic simulator and generic case types retained | No provider-backed invoice/mandate vertical slice |
| Scheduled generic sensors | `sensors/pollers.py` entry points currently return zero-scan placeholders | They are not working provider reconcilers; live abandonment uses the separate implemented demo dispatcher |
| Generalized contracts | Current session requires `razorpay_order_id`; Python/TypeScript live contracts remain order-specific | No `provider_entities` table, generalized session fields, entity-aware token union, or `/demo/scenarios` yet |
| Release checks | Existing Make targets, validators, tests, fixtures, builds, migrations, simulator/evals, and incident capture reused | Added an isolation wrapper, not a second implementation of the checks |

### Fixes implemented

1. **API-confirmed failure after abandonment now takes precedence.** The existing abandonment
   worker returned immediately when it found any case, so a later failed Checkout attempt could
   never promote an abandonment case unless a failure webhook arrived. It now rechecks provider
   truth for an abandonment case and shares `promote_abandonment_case` with webhook processing.
   Repeated checks do not append another reclassification or create another case/email action.
   The regression in `tests/test_api_august_31.py` first failed on the old code, then passed with
   refreshed diagnosis, audit replay parity, original-order success, same-case closure, and pending
   email cancellation.

2. **First webhook on an empty database now creates its merchant before the observation.** The
   clean PostgreSQL gate exposed a foreign-key failure in `payment_attempt_observations` before
   `record_signal` could create the merchant. Extracted the existing savepoint-safe merchant
   creation from `ensure_principals` into `ensure_merchant` and reused it before observation
   persistence. Three regressions in `tests/test_webhooks.py` enforce SQLite foreign keys for
   first-event failure, capture, and order-paid payloads; all failed before the fix. They also
   check worker redelivery and that a success observation alone creates no recovery case.

3. **Repeatable isolated release entry point.** `make release-gate-isolated` invokes
   `scripts/run_isolated_release.py`, which copies the current working source without credential
   files or historical artifacts and calls the existing `make release-gate-automated`. Only the
   copied infrastructure addresses and output locations change. Each run has distinct image names,
   Compose project, database volume, broker, and loopback ports. Runtime mode is `simulation`,
   external provider keys are empty, and model/outbound-email switches are off in the services.
   Logs and synthetic outputs are retained under `artifacts/baseline/`; only the isolated containers
   are stopped. The test volume and temporary source copy remain available for inspection.
   `make release-evidence` remains a separate read-only check of existing provider artifacts.

### Automated evidence

Initial baseline: `make lint test-coverage` passed **244 tests, 87.98% coverage**. That suite used
the existing in-memory SQLite fixtures, which did not enforce the merchant foreign key. A green
unit suite therefore did not establish a green clean-PostgreSQL release.

Saved initial/red regression logs:
`artifacts/baseline/regressions-2026-09-03/`.

The first complete invocation of the new isolation command is retained at
`artifacts/baseline/leakproof-release-j05hkyv5/`. Builds, fresh/reused migrations, lint, 245 tests
(88.06% coverage), dashboard check/build, and public bundle scan passed; foundation verification
failed with three committed inbox rows, zero processed rows, and the merchant foreign-key error.
This was fixed in code, without seeding a merchant to hide the defect.

Earlier temporary harness attempts also exposed two harness configuration issues: Docker Desktop
did not make the internal-network host ports reachable, and a dummy webhook secret differed from
the existing test fixture. The retained runner uses an independent bridge network and the existing
test webhook secret; these were harness issues, not provider or application evidence.

Final full-run evidence directory:
[`artifacts/baseline/leakproof-release-pfi4zn63/`](../artifacts/baseline/leakproof-release-pfi4zn63/).
`summary.json` records exit code 0 and cleanup exit code 0; run time was 03:15:53–03:17:11 UTC
(08:45:53–08:47:11 IST). `release-gate.log` contains the complete underlying command output.

| Check | Final result |
|---|---|
| API and dashboard image builds | PASS: both Docker images built under isolated project names |
| Fresh and reused migrations | PASS: both upgrades reach `0010_payment_attempts`, 25 public tables; separate disposable migration database |
| Ruff and full Python suite / 85% coverage floor | PASS: 248 tests, 88.07% coverage (four new parameterized regression cases) |
| TypeScript and production dashboard build | PASS: `tsc --noEmit` and Next.js production build |
| Public bundle credential/canary scan | PASS: four test credential/canary values absent from built public assets; real credentials were not loaded |
| Foundation twice: inbox, replay, PostgreSQL append-only rejection | PASS twice on the same isolated stack: three processed inbox rows per run, `DETECTED → ASSIGNED → SIGNAL → SIGNAL`, duplicate rejected, replay matches, UPDATE/DELETE rejected |
| Seed-42 batch replay | PASS: 787 cases; second execution leaves 5,831 events, 992 actions, 163 attributions and scoreboard unchanged (`batch.json`) |
| Frozen evaluations | PASS: 120 simulator regression cases, 15 decision-quality cases, 64 injection/benign cases with zero bypasses (`evals.json`) |
| Scoped synthetic AI incident / model-disabled fallback | PASS: 47/52 current failures vs 4/100 baseline; 47 matching cases affected, zero unrelated; disabled model opens zero suppressions and audits `NO_ACTION` (`cohort-incident.json`) |
| Security and acceptance subsets | PASS: 19 security tests and 17 acceptance/foundation/artifact tests |
| Existing provider artifact validator | PASS: two sanitized complementary hero-path exports |

**Release-claim discrepancy found:** the existing README headline seed-42 numbers are not the
current simulator run's numbers. The new `sim_v4_42_d1c5ff8a` artifact reports 148/694 treatment
recoveries (21.33%), 15/93 holdout recoveries (16.13%), +5.197 percentage points of rate lift,
764,938,114 paise gross treatment recovery, and **−893,608,337 paise simulated net economic value**.
The README headline instead lists 161/712, 11/75, and +7.95 points. The existing gate checks replay
and safety/quality invariants, not equality with the README table. Measurement code and historical
exports were retained; align the release narrative with the actual versioned artifacts before
submission. These are synthetic results, including the negative economic estimate, not realized
merchant revenue.

### Provider rehearsal evidence, kept separate

Existing files were validated with
`make release-evidence` (`--require-live --require-both-hero-paths`) and preserved:

| Artifact | Historical evidence | Limits |
|---|---|---|
| `artifacts/api-acceptance/hero-path-checkout-abandon.json` | Exported 2026-09-01 18:58 UTC; schema `2026-09-04`; passed; one closed case; 50,000 paise recovered; browser dismissal, Razorpay unpaid-order read, recovery check, and successful Checkout verification | Preview-only email; current legacy `LIVE_PROVIDER_VERIFIED` label; not recaptured against these fixes |
| `artifacts/api-acceptance/hero-path-payment-failure.json` | Exported 2026-09-01 19:16 UTC; schema `2026-09-04`; passed; one closed case; 50,000 paise recovered; Razorpay API failure detection and successful Checkout verification | Preview-only email; not evidence for the later-failure-after-abandonment regression fixed here |

Validator success verifies the stored schema, required checks, declared provenance, complementary
case types, and redaction. It does not independently replay those historical transactions against
Razorpay or prove that this working tree was used. No fresh payment or provider call was made for
this step. Historical artifacts were not relabelled or replaced with simulated exports.

The new plan's `LIVE_TELEMETRY_PROVIDER_RECONCILED` and `CONTRACT_VERIFIED` labels are not yet in
the existing three-value provenance enum. Their implementation and evidence gating remain part of
the extension, rather than a reason to discard the valid legacy abandonment capture.

### Remaining milestones

| Milestone | Status after this preparation | Exit still required |
|---|---|---|
| 0 — Provider capability spike | Not started | Confirm test-account invoice/subscription entitlements and HTTPS webhook event availability; capture sanitized official/account payloads and record unsupported mandate behavior |
| 1 — Multi-entity foundation | Not started | Forward-only session/entity-correlation migration, typed adapters/signals, generalized contracts and purpose-bound tokens; retain current payment tests |
| 2 — Checkout abandonment acceptance | Core implementation and historical capture already exist | Scenario entry, visible delayed recheck, telemetry-specific evidence label, deterministic contract capture, updated provider-reconciled artifact |
| 3 — Invoice slice | Simulation only | Original-invoice recovery, overdue/expiry detection, partial/full-payment reconciliation, UI and artifact |
| 4 — Subscription slice | Initial normalizer plus simulation only | Per-cycle correlation, Razorpay-owned retries, customer-authorized method update, charged/active reconciliation and artifact |
| 5 — Mandate specialization | Simulation only | Provider reason allowlist, same-cycle precedence, separate authorization/revenue outcomes; disable interaction if only contract evidence is possible |
| 6 — Portfolio release | Baseline automated gate now green | Cross-surface chaos/redaction/capability gates, five appropriately labelled artifacts and recruiter demo |

### Blockers and next step

- No remaining blocker to baseline code completion or starting milestone 0. A green automated
  baseline is separate from completing all provider rehearsals or the portfolio release.
- No account-level evidence has been gathered for new surfaces. Do not infer entitlements, event
  availability, or deterministic mandate behavior from the existence of order-payment credentials.
- Allowlisted Resend recipient delivery remains a provider rehearsal gap. The two existing
  artifacts both pass the current validator with previews; that does not satisfy the runbook's
  additional instruction to demonstrate delivery across the rehearsals.
- The current deployed/running demo was not updated. A fresh test-mode rehearsal of changed code
  requires a separately requested update and a human completing Checkout. Existing signatures,
  transactions, and audit data must remain intact.
- Refresh or explicitly version the README simulation headline before release; the current run
  does not substantiate its historical figures (see discrepancy above).
- Optional backup recording was not produced in this step; no recording is required for baseline
  automated completion.

**Next step:** milestone 0, the bounded Razorpay test-account capability/evidence spike. Revalidate
the official references listed in the implementation plan, establish an evidence matrix for
invoices/subscriptions/mandates, and capture sanitized fixtures before promising new UI behavior.
Then begin milestone 1 while continuing to run `make release-gate-isolated`. Keep autonomous debits,
production outreach, and production credentials outside the implementation.

### Preservation

The pre-existing README change and untracked multi-surface plan were retained. Existing acceptance,
AI incident, simulator, and committed audit artifacts were not overwritten. No command stopped or
recreated the original `leakproof` services or removed their database volume. No database downgrade,
audit deletion, commit, push, or deployment was performed.
SHA-256 comparison confirmed the README, multi-surface plan, both provider acceptance JSONs, and
the existing AI/simulator artifacts were byte-for-byte unchanged; results are saved in
`artifacts/baseline/leakproof-release-pfi4zn63/preservation.json`. The six original `leakproof`
containers remained running (API/PostgreSQL/Redis healthy) after isolated cleanup. Committed
sample audit files remain unchanged in the Git diff.

## 2026-09-03 — Provider-capability investigation

**Investigation complete; account lifecycle verification remains open.** This entry supersedes the
earlier milestone-0 “not started” status. The plan now separates payable aging from invoice expiry,
pins recurring failures to invoice obligations, qualifies mandate evidence by method/product, and
requires one attribution ledger across surfaces. No application functionality for milestones 1–5
was introduced. Their provider scenarios remain disabled/simulated until the respective gates pass.

### Implementation inspected and reused

- Reused `Settings` test credentials and `RazorpayPaymentProvider._request_json` for bounded GETs,
  with one attempt per request, existing auth/timeout/error handling, and no new provider adapter.
  No secrets, raw entities, customer details, provider identifiers, or hosted URLs were retained.
- Retained the signed durable webhook inbox, payment/abandonment precedence, original-order
  recovery, append-only case/action spine, and existing tests/fixtures. No completed baseline work
  was repeated or rewritten; historical acceptance artifacts remain historical evidence.
- Inspected `sensors/normalizer.py`, `sensors/processor.py`, `services.py`, provider contracts,
  database constraints and existing webhook/measurement tests. The initial subscription normalizer
  uses `paid_count` as `cycle_number`, resource creation time as occurrence time, and an amount
  fallback that is not an invoice balance. Existing per-case attribution uniqueness and
  customer/amount matching do not prevent cross-surface credit. These are explicit milestone-1
  gates in plan sections 6.4 and 17.2, not fixes claimed in this investigation.

### Read-only account evidence

Probe started **2026-09-03 03:26:42 UTC (08:56:42 IST)** using configured `rzp_test_` credentials.
The sanitized [probe record](evidence/razorpay-capability-readonly-2026-09-03.json) records paths,
bounded query parameters, returned counts/statuses and limitations. Four GETs were attempted:

| Read | Observed result | What it establishes |
|---|---|---|
| `/v1/orders?count=1&skip=0` | Successful JSON collection; one paid order | Current key can read this Orders collection; not a new payment rehearsal |
| `/v1/invoices?count=10&skip=0` | Successful JSON collection; zero invoices returned | Invoice collection read accepted; no create/issue/pay/expire capability proven |
| `/v1/plans?count=10&skip=0` | HTTP 401; adapter `authentication_failed` | Plan access unresolved; reason cannot be reduced to “Subscriptions disabled” from this response |
| `/v1/subscriptions?count=10&skip=0` | HTTP 401; adapter `authentication_failed` | Subscription access unresolved; no lifecycle or enabled-method evidence |

Counts describe these bounded responses, not an exhaustive account inventory. The configured HTTPS
base and webhook secret are present; no Dashboard registration/delivery inspection was performed.
No known linked recurring customer/token was available for a scoped token read. No POST/PATCH/PUT/
DELETE provider requests, resource creation, hosted payment visits, webhook edits, Dashboard charge
actions, or recipient/support contact occurred. Documentation was fetched separately from official
Razorpay pages, including their published table content where the text renderer omitted tables.

### Capability matrix

“Documented” below is a research finding, not the runtime `CONTRACT_VERIFIED` label. No new adapter
contract suite or signed account fixture was created; none of the new surfaces earns that label
merely from this investigation. Full source links and decisions are in
[plan sections 16–17](../MULTI_SURFACE_RECOVERY_IMPLEMENTATION_PLAN.md#16-official-sources-checked-3-september-2026).

| Capability | Official support / supported decision | Account evidence / remaining gate | Current evidence level |
|---|---|---|---|
| Existing payment and abandonment | Reuse working order/payment recovery | Existing historical artifacts; one current Orders read; no fresh closure capture | Historical provider evidence retained, not upgraded |
| Invoice APIs | Create/issue/fetch/list and state-limited mutations; typed read/setup boundary planned | Empty invoice collection read; writes and lifecycle untested | Documented + account read only; new recovery remains simulated |
| Payable overdue invoice | App-owned aging threshold on `issued`/`partially_paid`; original hosted URL while unexpired | No invoice to prove hosted payment/partial settlement | Documented; not account verified |
| Expired/non-payable invoice | `invoice.expired` stops payment CTA; merchant review; no expiry extension after expiry | No expiry event captured | Documented; not account verified |
| Invoice events/closure | Partial/paid/expired events; invoice/payment reconciliation, incremental amounts only | HTTPS registration, signatures, event delivery and full/partial payment pending | Documented; not contract or account verified |
| Subscription APIs/events | Plan/subscription CRUD subset plus subscription invoice listing and lifecycle webhooks | Plans/Subscriptions 401; no reusable plan, invoice-cycle mapping or method entitlement proven | Documented; account access unresolved |
| Subscription recovery | Card-update Checkout or supported hosted method transition; provider-owned retries; old arrears require their own settlement | Manual charge is method-limited; no update/charge rehearsal available | Documented; interactive path gated |
| Broken mandate | Candidate linked token cancellation, scoped eMandate inactive-mandate reason, or reconciled token expiry; explicit negative evidence rules | Recurring Payments vs Subscriptions linkage, token visibility and reproducible event emission unresolved; live allowlist empty | Documented candidates only; simulated |
| Multiple surfaces on one debt | Canonical invoice obligation, same-case promotion, unique captured-payment ledger, no money for authorization/activation | Cross-surface DB constraints, reconciliation and tests are milestone-1 work | Design decision, not provider behavior or implemented protection |

### Decisions resolved before implementation

1. **Invoice state governs the action.** The plan has an exhaustive state/action table, including
   draft, issued, partially paid, paid, expired, cancelled/deleted, and unknown/conflicting state.
   Overdue is a local aging rule; expiry is a provider payment cutoff. No replacement resource is
   silently created for expired debt. See [plan 17.1](../MULTI_SURFACE_RECOVERY_IMPLEMENTATION_PLAN.md#171-invoice-aging-expiry-and-actions).
2. **The failed cycle is an invoice, not a counter.** Resolve provider relationships even when a
   pending/halted payload lacks payment data. Multiple unpaid invoices can coexist. Missing or
   ambiguous cycle evidence blocks contact and attribution. See [plan 17.2](../MULTI_SURFACE_RECOVERY_IMPLEMENTATION_PLAN.md#172-available-apievent-contracts-and-failed-cycle-identity).
3. **Mandate classification needs precise evidence.** The reason `mandate_not_active` and the
   superficially similar `payment_mandate_not_active` have different documented meanings. Token
   rejection is registration failure; subscription cancellation alone is not mandate diagnosis.
   No generic decline or plain halt qualifies. See [plan 17.3](../MULTI_SURFACE_RECOVERY_IMPLEMENTATION_PLAN.md#173-broken-mandate-evidence-precise-method-scoped-not-yet-account-enabled).
4. **One obligation cannot earn four recoveries.** Linked payment, invoice, subscription and mandate
   events enrich/reclassify one case. Unique payment settlement entries prevent duplicated credit
   even across different event IDs or sessions; partial totals are never added twice. Authorization
   repair and active subscription do not pay old invoices. Existing customer/amount fallback must
   not apply to new surfaces. See [plan 6.4](../MULTI_SURFACE_RECOVERY_IMPLEMENTATION_PLAN.md#64-define-deduplication-and-precedence-before-implementation).
5. **Documentation gaps are gates.** The broad test guide and more specific retry guide do not
   justify promising that halted -> active clears arrears. Domestic-card manual charging is
   excluded, and method-update restrictions/test-resource limitations need an actual account
   rehearsal. See [plan 17.4](../MULTI_SURFACE_RECOVERY_IMPLEMENTATION_PLAN.md#174-recovery-restrictions-manual-tests-and-documentation-gaps).

### Manual setup and rehearsal checklist — not performed in this step

These actions require a later authorized setup/rehearsal. Do not run Dashboard test actions as
read-only checks. Account prerequisites below remain unchecked even when an official guide describes
them; no live-mode activation or production credentials are required by this project.

- [ ] **Account owner / Dashboard:** confirm the selected account and Test Mode; resolve Plans and
      Subscriptions 401 and verify enabled card/UPI/eMandate methods. Record the result without
      exposing keys. Do not assume that a working Orders key grants each product.
- [ ] **Dashboard webhook review/setup:** inspect existing endpoint/event selection and delivery
      health. In a separately authorized change, add only the required invoice/subscription/token
      events available for that account, preserving existing payment events and secrets. Capture
      signature-verified delivery; existence in documentation is insufficient.
- [ ] **Controlled notifications:** verify provider notification settings before creating test
      resources. Use invoice `sms_notify=false` and `email_notify=false` for API setup and inspect
      subscription `customer_notify`/Dashboard lifecycle settings. Confirm behavior with designated
      test recipients later; assume neither all lifecycle messages nor all retries are suppressible
      from a single flag. Retain Leakproof's allowlist and outbound gates.
- [ ] **Plan/setup (Dashboard or API):** select one reusable test plan and record a private operator
      configuration reference. Prepare separate authorization/update and charged-cycle resources as
      needed; create no plan per demo run. No reusable plan is currently verified.
- [ ] **Invoice setup (API or Dashboard):** prepare a non-GST invoice with explicit aging rule and
      sufficient future expiry. For partial-payment testing enable partial payment at setup. Pay
      partial then remaining balance as a human on the hosted invoice; capture events and reads.
- [ ] **Invoice expiry (provider time, not a Dashboard test-charge button):** use a separate unpaid
      invoice, respect the documented minimum future expiry, wait and capture expiry. Verify the
      app removes its CTA; do not attempt to extend an already expired invoice. GST-specific setup,
      if later needed, uses Dashboard rather than the non-GST API path.
- [ ] **Subscription authorization (human Checkout):** authorize a fresh controlled test resource.
      Keep token/refunded authorization amounts out of revenue; capture exact entity relationships.
- [ ] **Subscription failure/success (manual Dashboard only in the reviewed test workflow):** use
      **Charge this now**, choosing success or failure; four consecutive failures exercise halt.
      Preserve one invoice through its retries. The public API index does not document an equivalent
      test-charge endpoint; do not automate private Dashboard endpoints.
- [ ] **Halted next cycle (manual Dashboard):** use **Issue Invoice** to demonstrate another unpaid
      invoice without a charge; prove old-cycle isolation rather than using `paid_count` as a key.
- [ ] **Arrears recovery (human + eligible Dashboard action):** update the method, then inspect the
      old invoice. Where supported, **Attempt Charge** on that exact issued invoice proves arrears
      settlement; domestic cards are excluded. If unavailable, retain merchant review/active with
      unpaid arrears and do not claim a completed recovery loop.
- [ ] **Test timing/update constraints:** perform subsequent test card charges within three days of
      token creation. Use a separate pre-subsequent-charge subscription for general update tests;
      verify card-change behavior separately rather than extrapolating from this limitation.
- [ ] **Mandate evidence (method-specific, no proven test simulator):** establish token-to-
      subscription/invoice linkage and capture a real invalid-authorization event or read. For UPI,
      token list visibility may need `save_vpa`; verify account access. Customer cancellation may
      require the UPI app/bank portal, not Dashboard, and may terminate the subscription. Merchant
      cancellation or repeated generic test failures must not be relabelled as revoked mandate.
- [ ] **Acceptance:** retain sanitized real lifecycle captures; run adapter/normalizer contracts,
      cross-surface duplicate/order/partial-payment/concurrency tests, and entity-specific closure
      validation before changing capability labels. Keep unsupported interactions disabled.

**Next implementation:** milestone 1 can proceed using these contracts and existing tests while
account prerequisites remain explicit. Resource setup, webhook changes, and provider lifecycle
rehearsals are still outstanding; this step did not satisfy them through documentation.

### Validation and preservation

Reused the existing `tests/test_webhooks.py`, `tests/test_api_august_30.py`,
`tests/test_api_august_31.py`, and `tests/test_measurement.py`: **40 tests passed**. Their fixtures
isolate credentials and use fake/mock providers. They verify the retained baseline, not the new
invoice/subscription/mandate contracts. No new tests were necessary for this documentation-only
step. Full release/build/migration gates were not repeated because runtime code was not changed.

The two Markdown documents and the sanitized GET probe record are the only deliverables changed.
Checked JSON structure, local Markdown targets, credential/provider-ID redaction and Git whitespace.
No database migration, service restart, deployment, commit or push occurred; existing acceptance
artifacts and application/tests were preserved.
