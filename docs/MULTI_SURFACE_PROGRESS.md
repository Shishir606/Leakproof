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
