# Leakproof: Four-Day Critical and High-Priority Improvement Plan

## Purpose

This document converts the current project audit into an implementation plan for the four days
before submission. It is written for Codex to execute against this repository. The goal is not to
add more feature breadth. The goal is to make the existing project defensible as a safe, meaningful
AI revenue-recovery agent with a short, reliable, evidence-backed demonstration.

The implementation must preserve the project's strongest properties:

- Razorpay remains the source of truth for payment state.
- AI may resolve ambiguity and propose an intervention, but it may not move money, contact a
  customer, bypass policy, or override a stopping rule.
- All external actions remain idempotent, bounded, gated, and auditable.
- Live, synthetic, estimated, and provider-verified data must never be presented as equivalent.
- No new recovery surfaces should be added during these four days.

## Submission definition of done

The release is ready only when all of the following are true:

1. A reviewer can complete a checkout-abandonment recovery and a payment-failure recovery from a
   fresh browser, ending with the original Razorpay order paid, the same case closed, pending
   contacts cancelled, and the live recovered amount updated.
2. AI makes one material, visible, bounded decision: it diagnoses a multi-event payment anomaly
   and proposes a scoped intervention that deterministic validation accepts, rejects, or narrows.
3. The live cohort scanner uses observed success/failure attempt counts. It never derives a
   denominator from a declared failure-rate field.
4. Global case, scoreboard, evaluation, replay, and audit endpoints are not anonymously accessible
   in the public deployment. Public demo access remains session-token-bound and sanitized.
5. Every dashboard metric declares its provenance. Synthetic output uses words such as
   `simulated`, `estimate`, and `assumption`; provider-verified live recovery uses `verified`.
6. The synthetic evaluation reports uncertainty and assumptions instead of presenting one seeded
   result as realized revenue.
7. The AI evaluation uses separately authored, noisy and negative cases, includes a rules-only
   baseline, and does not use perfect F1 as the success target.
8. `make release-gate` passes from a clean database and includes the repository's advertised
   foundation verification, complete automated suite, dashboard build, security tests, evaluation
   gates, and acceptance-artifact validation.
9. Two sanitized successful acceptance artifacts and a short backup demo recording exist. No API
   key, recipient, payment detail, order ID, recovery token, session token, or signed URL is present
   in them.

## Non-goals for these four days

Do not spend time on:

- Real PSTN/voice infrastructure.
- New subscription, invoice, mandate, WhatsApp, SMS, CRM, or helpdesk integrations.
- A general-purpose agent framework or autonomous tool loop.
- LLM-written customer messages.
- A complete identity platform. Implement a safe buildathon operator boundary and clearly document
  how production identity would replace it.
- More dashboard pages. Improve the existing Live Demo, Scenario Lab, and Case Timeline.

## Problem register

### C0 — AI is currently non-essential

**Current problem**

The live model explains a decision after deterministic diagnosis and planning have already
completed. Removing Luna would barely change the recovery workflow. The more consequential cohort
path still defaults to `DeterministicCohortTransport`, including the scheduled worker path, despite
the integration plan requiring a live OpenAI adapter.

**Required solution**

Give AI responsibility for bounded multi-event root-cause analysis and intervention proposal:

1. Deterministic code aggregates sanitized payment-attempt observations and applies minimum
   evidence thresholds.
2. AI receives only qualified aggregate slices and returns a strict structured proposal.
3. Deterministic validation proves that the proposal refers only to supplied evidence, uses an
   allowed scope and action, satisfies confidence/sample requirements, and stays within TTL limits.
4. The existing guardrail/policy layer approves, rejects, or narrows the proposal.
5. Only the validated decision may open a scoped circuit breaker, delay retries, alert the merchant,
   or do nothing.

The useful AI output must be visible as a separate proposal from the deterministic verdict. The
case-level explanation may remain, but it is secondary.

### C1 — Five-surface language can overstate what is live

**Current problem**

Five recovery surfaces are represented in the simulator, but the complete live provider loop covers
one-time payment failure and checkout abandonment. A reviewer can interpret the current headline as
claiming that invoice, subscription, mandate, and voice paths are live integrations.

**Required solution**

Introduce explicit capability and provenance language everywhere:

- `LIVE_PROVIDER_VERIFIED`: Razorpay payment failure and checkout abandonment.
- `SIMULATED_END_TO_END`: invoice overdue, subscription halt, and voice/promise.
- `ARCHITECTURE_READY`: code boundary exists but a live provider is not connected.

The README, dashboard navigation, Scenario Lab, API responses, demo script, and submission text must
use the same vocabulary. The submission headline should describe one live recovery loop and five
validated expansion surfaces, not five live integrations.

### C2 — Synthetic recovery impact is circular

**Current problem**

Organic recovery rates and intervention effects are inputs to the simulator. The resulting lift is
useful for validating the measurement pipeline but cannot prove real-world product lift. One seed,
one exact point estimate, and phrases such as `Net value created` make the evidence look more certain
than it is.

**Required solution**

- Rename synthetic financial outputs to `Simulated ... estimate`.
- Show the assumptions hash and the most important recovery-effect assumptions in the UI.
- Run several seeds across pessimistic, expected, and optimistic treatment-effect scenarios.
- Report median, range, and a confidence interval or bootstrap interval.
- Keep live provider-verified recovered money in a separate live metric; never combine it with the
  synthetic estimate.
- Replace the default 1.0 revenue margin in economic claims with configurable contribution margin.

### C3 — The hero flow is not yet proven through recovered money

**Current problem**

The live flow convincingly reaches detection, diagnosis, planning, and a recovery action, but the
release evidence does not yet prove both hero paths through successful payment, same-case closure,
action cancellation, and scoreboard update. The 30-second abandonment wait and indirect recovery
navigation weaken a short review.

**Required solution**

- Add an obvious `Continue recovery` CTA for the current case.
- Use a 5–10 second abandonment delay only in the public demo configuration; retain the production
  default separately.
- Display an explicit progress sequence: `order → leak → diagnosis → proposal/gate → recovery →
  verified payment`.
- Split `This session` metrics from aggregate demo-environment metrics.
- Add `Start a new demo` that clears only browser session state and creates a new server session; it
  must not delete historical audit data.
- Capture two manual provider-verified rehearsals: checkout dismissal and Razorpay test payment
  failure.

### C4 — Public operational APIs lack a sufficient operator/merchant boundary

**Current problem**

Session-specific demo projections are signed and sanitized, but global cases, scoreboards,
evaluations, replay, and audit endpoints are intended for direct dashboard access. A public live
deployment must not allow anonymous enumeration or cross-merchant access.

**Required four-day containment solution**

Implement a server-side operator boundary suitable for the buildathon deployment:

- Add a constant-time-checked bearer credential configured through
  `LEAKPROOF_OPERATOR_API_TOKEN`; require at least 32 random bytes in `live_demo` mode.
- Resolve an `OperatorPrincipal` on the API server. The principal must contain the permitted merchant
  scope; route handlers must derive merchant scope from the principal rather than request input.
- Protect global `/cases`, `/scoreboard`, `/evals`, replay, detailed audit, exception, and acceptance
  endpoints.
- Keep health checks, signed provider webhooks, demo-session creation, token-bound demo projection,
  and token-bound recovery bootstrap public.
- The Next.js server may attach the operator credential for server-rendered operator pages. Mark the
  backend client server-only and prove the token is absent from browser HTML and JavaScript.
- For the public buildathon deployment, either hide Case Timeline navigation from unauthenticated
  users or expose only a curated synthetic, identifier-free public Scenario Lab projection.

This is a buildathon containment boundary, not a substitute for production OAuth/RBAC. Document
that production must use real merchant identity and authorization.

### H1 — Payment-degradation analysis lacks an observed denominator

**Current problem**

The cohort scanner primarily sees failure cases and can derive attempts from a failure rate embedded
in event evidence. That cannot establish a real change in success rate.

**Required solution**

- Add a durable `payment_attempt_observations` table with a migration.
- Record sanitized provider observations for both `payment.failed` and successful
  `payment.captured`/`order.paid` paths, deduplicated by provider payment/order identity.
- Store merchant, provider event identity, event time, outcome, method, issuer/bank, safe BIN bucket
  when available, checkout version/step when available, and source. Do not store customer contact
  data in this table.
- Reconcile duplicate success events so one payment is one successful attempt.
- Aggregate current-window and historical-baseline attempts/failures directly from observations.
- Emit `INSUFFICIENT_DATA` and skip the model when sample or baseline requirements are not met.
- Delete support for deriving attempt counts from `cohort_failure_rate` in event evidence.
- Seed deterministic attempt observations for Scenario Lab without mixing them into live metrics.

### H2 — Current evaluations are self-referential

**Current problem**

The deterministic detector and synthetic evaluation cases share closely aligned rules. Perfect F1
therefore demonstrates regression consistency, not generalization. The injection suite is useful for
schema boundaries but should not be presented as broad prompt-injection resistance when the model
does not ingest arbitrary customer text.

**Required solution**

- Create a separately authored frozen cohort evaluation set containing noisy, incomplete,
  contradictory, multilingual-label, clean, and near-miss examples.
- Include issuer-specific, method-wide, gateway-wide, BIN, checkout-regression, payer-cluster,
  merchant-misconfiguration, and random-noise cases.
- Measure pattern accuracy, scope precision/recall, action appropriateness, false suppression rate,
  unsupported-evidence rate, schema validity, latency, and cost.
- Compare `rules-only`, `AI proposal before validation`, and `AI + deterministic validation`.
- Add explicit tests proving unsafe proposals are rejected and provider/model failure results in a
  safe `NO_ACTION`/merchant-review fallback.
- Rename the existing injection claim to describe exactly what it tests: structured-input boundary
  and output-schema/authority enforcement.
- Do not make `F1 == 1.0` a release requirement. Use realistic minimum gates and publish failure
  examples.

### H3 — Net-value economics are too optimistic

**Current problem**

The current economic view largely treats recovered revenue as value and subtracts only direct
intervention/model costs. It does not adequately represent merchant contribution margin, human
review, operational expense, discounts, refunds/chargebacks, or uncertainty.

**Required solution**

Report three separate measures:

1. `Incremental revenue estimate`.
2. `Incremental contribution-margin estimate`.
3. `Net economic-value estimate` after intervention, model, email/voice, and human-review costs.

Add declared configuration for contribution margin and human-review cost. Keep unknown costs visible
as exclusions rather than silently treating them as zero. Every synthetic economic response must
include assumptions, estimator, run count, and uncertainty metadata.

### H4 — The advertised release verification is stale and red

**Current problem**

`make verify-foundation` expects exactly three case events and an old sequence. The current pipeline
correctly adds `ASSIGNED`, creating four events. The command reports a misleading Celery timeout even
though all three unique webhooks were processed.

**Required solution**

- Update the verifier to assert the semantic sequence `DETECTED`, `ASSIGNED`, `SIGNAL`, `SIGNAL`.
- Stop using exact total event count as the proxy for worker completion. Independently wait for the
  three unique webhook rows to be processed, then assert required event kinds and ordering.
- Include diagnostic counts and last processing error in timeout output.
- Run the verifier from a clean and a reused database.
- Add `make release-gate` as the single documented submission command.

## Architectural invariants for implementation

Codex must preserve these invariants while editing:

1. **Provider truth wins.** Browser completion is advisory; only Razorpay success closes and
   attributes a live case.
2. **AI has proposal authority, not execution authority.** It receives no tools and cannot directly
   invoke an actuator.
3. **Strict structured output.** Model output uses enums, bounded strings, bounded TTL, and stable
   evidence slice IDs. Use `store=false`, bounded output tokens, timeout, retry, and cost limits.
4. **Evidence grounding.** AI returns `evidence_slice_ids`, not unverifiable free-form counts.
   Deterministic validation resolves the IDs and recomputes every threshold.
5. **Fail closed.** Invalid schema, unsupported scope, invented evidence, excessive TTL, low
   confidence, timeout, or exhausted budget cannot open a suppression or contact a customer.
6. **No PII in cohort analysis.** Only aggregate counts and allowlisted payment dimensions may enter
   the model request.
7. **Immutable audit separation.** Persist the raw structured proposal, validation verdict, policy
   verdict, and executed consequence as distinct events.
8. **No data provenance mixing.** Live session metrics query only live sessions. Scenario metrics
   query only one synthetic batch namespace.
9. **No secret in the browser.** Operator credentials and provider secrets remain server-side.
10. **No destructive demo reset.** Starting a fresh demo never deletes audit rows or provider events.

## Target AI contract

Use a contract equivalent to the following. Exact names may change to fit existing conventions, but
the semantics and restrictions must remain.

```json
{
  "pattern": "issuer_outage",
  "scope": {
    "issuer": "HDFC",
    "method": "netbanking"
  },
  "evidence_slice_ids": ["slice_current_hdfc_netbanking", "slice_baseline_hdfc_netbanking"],
  "confidence": 0.94,
  "recommended_action": "SUPPRESS_RETRIES",
  "ttl_minutes": 60,
  "rationale": "A scoped issuer/method failure spike is materially above its observed baseline."
}
```

Allowed patterns:

- `issuer_outage`
- `gateway_degradation`
- `method_degradation`
- `bin_rule_change`
- `checkout_regression`
- `payer_cluster`
- `merchant_misconfiguration`
- `no_material_anomaly`

Allowed recommendations:

- `SUPPRESS_RETRIES`
- `DELAY_RETRY`
- `ALERT_MERCHANT`
- `CHANGE_PAYMENT_METHOD_PROMPT`
- `NO_ACTION`

The validator must enforce an action-specific policy. For example, `SUPPRESS_RETRIES` requires a
qualified observed failure spike, supported scope, bounded TTL, and confidence at or above the
configured suppression threshold. `ALERT_MERCHANT` may use a lower confidence threshold but cannot
contact a customer or suppress unrelated cases.

## Four-day execution plan

---

## Day 1 — Establish trust boundaries and a green baseline

### Day 1 outcome

The repository has a truthful capability contract, protected operational APIs, and one reliable
release command. No AI or measurement work should begin on top of a red or publicly exposed base.

### 1. Fix foundation verification (H4)

Likely files:

- `scripts/verify_foundation.py`
- `Makefile`
- `README.md`
- `tests/test_webhooks.py`
- a new focused test for the verification query/sequence if helpful

Implementation:

1. Wait for three distinct webhook rows with `processed_at IS NOT NULL`.
2. Fetch the corresponding case and ordered audit events separately.
3. Assert one case, three processed webhook identities, and semantic event sequence containing
   `DETECTED`, `ASSIGNED`, and two `SIGNAL` events.
4. Improve timeout output with inbox count, processed count, event kinds, processing attempts, and
   safe error summaries.
5. Add `make release-gate` with, at minimum:

   ```text
   lint
   full Python test suite
   Python coverage floor
   dashboard typecheck
   dashboard production build
   foundation verification
   eval gates
   API security tests
   acceptance-artifact schema/redaction tests
   ```

6. The release target must return nonzero on any failed step.

Acceptance:

- `make verify-foundation` passes twice against the same running stack.
- Duplicate webhook delivery still creates one inbox row and no duplicate case signal.
- Postgres still rejects event update/delete.
- A forced worker failure produces actionable verifier output rather than a generic timeout.

### 2. Protect operator APIs and enforce merchant scope (C4)

Likely files:

- `src/leakproof/config.py`
- `src/leakproof/api/app.py`
- a new `src/leakproof/api/auth.py`
- `dashboard/lib/api.ts`
- `dashboard/lib/backend-proxy.ts`
- `dashboard/app/api/audit/[caseId]/route.ts`
- `docker-compose.yml`
- `.env.example`
- API contract/security tests

Implementation:

1. Add operator-token configuration and validation.
2. Add a FastAPI dependency that parses `Authorization: Bearer`, compares in constant time, and
   returns an operator principal with permitted merchant scope.
3. Apply it to all global operational endpoints.
4. Add merchant predicates to every protected case/detail/replay query. A valid credential without
   access to the target merchant must receive `404`, not an existence-revealing `403`.
5. Keep public demo endpoints token-bound as they are; do not place the operator token in their
   responses.
6. Attach the operator token only from the Next.js server backend client. Add `server-only` where
   appropriate.
7. Hide or disable operator-only navigation in a public build without an authenticated operator
   surface.

Security acceptance tests:

- Missing/invalid credentials receive `401`.
- Valid credential with the wrong merchant scope receives `404` for object routes and an empty or
  scoped collection for list routes.
- Signed session A cannot read session B.
- The operator token does not appear in rendered HTML, client bundles, API JSON, logs, or acceptance
  artifacts.
- Webhook, health, session-create, session-projection, checkout-event, and recovery-bootstrap paths
  continue to work with their existing boundaries.

### 3. Make capability and provenance explicit (C1)

Likely files:

- `README.md`
- `dashboard/app/scenario-lab/page.tsx`
- `dashboard/components/shell.tsx`
- dashboard API types
- scoreboard/demo response contracts

Implementation:

1. Add a `data_provenance` enum to relevant response contracts.
2. Require Scenario Lab to render only synthetic provenance and Live Demo to render only live-demo
   provenance; treat a mismatch as an error.
3. Replace ambiguous five-surface wording with a capability matrix.
4. Keep the synthetic banner persistently visible, not only above the fold.
5. Change public copy from `five live recovery surfaces` to `one live recovery loop; five simulated
   expansion surfaces` wherever applicable.

Day 1 exit criteria:

- Foundation verification is green.
- Security tests prove anonymous/global and cross-merchant access is blocked.
- The app cannot visually mix live and synthetic data.
- The working tree contains no generated secrets or acceptance tokens.

---

## Day 2 — Make AI consequential and ground it in real attempt data

### Day 2 outcome

AI proposes a material cohort intervention from observed aggregate data; deterministic code validates
it; the complete proposal-to-consequence chain is auditable. Live code no longer uses the
deterministic cohort substitute.

### 1. Add observed payment-attempt facts (H1)

Likely files:

- `src/leakproof/models/db.py`
- a new Alembic migration
- `src/leakproof/sensors/normalizer.py`
- `src/leakproof/sensors/processor.py`
- `src/leakproof/sensors/webhooks.py`
- `src/leakproof/diagnosis/tier2.py`
- simulator persistence/generation files
- webhook, cohort, deduplication, and migration tests

Implementation:

1. Create `PaymentAttemptObservation` with a unique provider dedupe key and indexes for merchant,
   observed time, outcome, issuer, and method.
2. Upsert failure observations from `payment.failed`.
3. Upsert success observations from `payment.captured`; reconcile `order.paid` without double
   counting the same payment/order success.
4. Store only safe aggregate dimensions. Normalize absent dimensions to explicit `unknown` values
   rather than inventing them.
5. Rewrite cohort aggregation to compute:

   - current attempts, failures, and observed failure rate;
   - historical attempts, failures, and baseline failure rate;
   - change magnitude and error-reason distribution.

6. Return no qualified candidate when the baseline or current sample is too small.
7. Remove `declared_rate`/`cohort_failure_rate` denominator logic.
8. Extend the simulator to persist attempt observations in a synthetic namespace so the existing
   issuer incident remains reproducible.

Acceptance:

- A captured event plus duplicate `order.paid` counts as one success.
- Duplicate failures count once.
- A failure-only stream without denominator/baseline yields `INSUFFICIENT_DATA`.
- The HDFC scenario produces exact observed attempt/failure counts from rows, not evidence claims.
- No live observation appears in a synthetic batch aggregate or vice versa.

### 2. Implement the live AI cohort provider (C0)

Likely files:

- `src/leakproof/providers/contracts.py`
- `src/leakproof/providers/openai.py`
- `src/leakproof/providers/factory.py`
- `src/leakproof/diagnosis/tier2.py`
- `src/leakproof/celery_app.py`
- `config/models.yaml`
- provider and Tier 2 tests

Implementation:

1. Define a provider-neutral cohort-analysis protocol.
2. Implement `OpenAICohortAnalysisProvider` with strict structured output, no tools, `store=false`,
   bounded tokens, timeout, maximum two attempts, per-run budget, and persisted request metadata.
3. Return stable evidence slice IDs in the request and response.
4. Use the deterministic transport only for simulation/tests. In `live_demo`, factory selection must
   return the OpenAI cohort provider or a clearly unavailable provider that degrades safely.
5. Inject the provider into `run_cohort_scan`; do not let that function silently instantiate the
   deterministic substitute in live mode.
6. Update the Celery scheduled scan to resolve and pass the configured provider.
7. Preserve the case-insight provider, but label it as explanatory rather than authoritative.

### 3. Add deterministic proposal validation and audit separation (C0)

Likely files:

- `src/leakproof/diagnosis/tier2.py`
- `src/leakproof/audit/timeline.py`
- model/domain files if new event kinds or tables are needed
- `dashboard/components/live-demo-dashboard.tsx`
- case-timeline presentation

Implementation:

1. Validate schema, evidence IDs, recomputed thresholds, scope, action enum, confidence, and TTL.
2. Reject scope expansion. The proposed scope must be equal to or narrower than a supported supplied
   slice and must not contain unknown dimension keys.
3. Persist distinct records/events for:

   - `AI_PROPOSED`
   - `AI_PROPOSAL_REJECTED` or `POLICY_VALIDATED`
   - `SUPPRESSION_OPENED`, `RETRY_DELAYED`, `MERCHANT_ALERTED`, or `NO_ACTION`

4. On provider/model failure, persist a degraded decision and execute no AI-originated suppression
   or customer contact.
5. Display proposal and verdict separately in the dashboard and timeline.

Day 2 acceptance scenarios:

1. **Issuer outage:** AI proposes scoped suppression; validation accepts it; matching retries are
   cancelled; unrelated issuer/card cases continue.
2. **Clean noise:** AI returns `NO_ACTION`; nothing is suppressed.
3. **Invented scope:** AI mentions a scope absent from the payload; validation rejects it.
4. **Excessive TTL:** validation rejects or clamps only if policy explicitly permits clamping; record
   the decision.
5. **Model outage/invalid schema:** recovery continues deterministically, no suppression opens, and
   the failure is visible and cost-audited.

Day 2 exit criteria:

- Removing or disabling the AI changes the cohort decision for ambiguous qualified incidents, while
  safety remains unchanged.
- No model output can directly call an actuator.
- Live mode contains no deterministic-model masquerade.
- The HDFC demonstration visibly shows `AI proposal → deterministic validation → scoped action`.

---

## Day 3 — Make measurement credible and the recovery demo undeniable

### Day 3 outcome

Synthetic results are clearly estimates with uncertainty and realistic economics. The live demo has
a fast, session-specific path to recovered money. AI evaluations measure useful decision quality
against a separately authored set.

### 1. Correct financial claims and economics (C2, H3)

Likely files:

- `simulator/params.yaml`
- `config/measurement.yaml`
- `src/leakproof/measurement/scoreboard.py`
- scoreboard API/domain types
- `scripts/run_batch.py`
- a new sensitivity/multi-seed runner
- `dashboard/app/scenario-lab/page.tsx`
- measurement tests

Implementation:

1. Add declared contribution margin, human-review unit cost, and documented optional/excluded costs.
2. Separate incremental revenue, contribution margin, and net economic value.
3. Add a multi-run script using several seeds and three treatment-effect multipliers. It must not
   pollute live tables; use isolated batch namespaces or a temporary evaluation database.
4. Report median, min/max, and an interval for lift and economic estimates.
5. Add `assumption_hash`, seed count, estimator, and uncertainty fields to the response.
6. Rename dashboard cards and README language:

   - `Gross recovered` → `Simulated gross recovery`
   - `Net value created` → `Simulated net economic-value estimate`
   - `Measured recovery lift` → `Simulated treatment-vs-holdout lift estimate`

7. Add an expandable assumptions panel showing contribution margin, intervention effects, holdout
   fraction, attribution window, and excluded costs.

Acceptance:

- Changing contribution margin changes contribution/net value but not recovered revenue.
- Pessimistic treatment effects visibly reduce or eliminate estimated lift.
- Live recovered amount never appears in the synthetic estimate.
- A scoreboard response without provenance or assumptions fails schema validation.

### 2. Replace self-referential AI evaluation (H2)

Likely files:

- `evals/cohort/` fixtures
- `src/leakproof/evals/runner.py`
- `scripts/run_evals.py`
- `evals/baseline.json`
- evaluation tests and dashboard copy

Implementation:

1. Preserve the current generated suite as `simulator_regression`; stop calling it a generalization
   evaluation.
2. Add a frozen, manually authored decision set not produced by simulator thresholds.
3. Run and report three systems:

   - rules-only baseline;
   - raw AI proposal;
   - AI plus deterministic validator.

4. Score root-cause pattern, exact/safe scope, recommendation appropriateness, unsupported evidence,
   false suppression, schema validity, cost, and latency.
5. Save a small set of failure examples with sanitized inputs and expected/actual decisions.
6. Update release gates to enforce safety metrics strictly and quality metrics realistically. Suggested
   starting gates:

   - false suppression rate <= 2%;
   - unsupported-evidence acceptance = 0;
   - invalid action execution = 0;
   - schema-valid responses >= 98% including retry;
   - safe fallback = 100%;
   - scope precision and root-cause F1 must exceed the frozen rules baseline by a declared margin.

Do not choose thresholds merely to make the current output pass. Record the initial baseline first.

### 3. Improve and rehearse the hero UX (C3)

Likely files:

- `dashboard/components/live-demo-dashboard.tsx`
- `dashboard/components/razorpay-checkout.tsx`
- `dashboard/lib/demo-types.ts`
- demo projection/service files
- `dashboard/app/globals.css`
- API acceptance tests

Implementation:

1. Add a session progress rail with the six hero stages.
2. Put a primary `Continue recovery` link/button beside the current recovery action. It must use the
   signed, order-bound recovery flow and must not expose the token in logs or audit exports.
3. Add `This session` metrics: detected amount, recovered amount, state, recovery latency, provider
   failures, and AI cost. Move aggregate environment metrics below or into a secondary panel.
4. Add `Start a new demo` by clearing only local active-session state and requesting a new session.
5. Configure 5–10 second abandonment only for the demo deployment.
6. Add visible waiting copy so the reviewer knows why the server recheck exists.
7. Ensure a verified payment immediately changes the current session to recovered, cancels pending
   actions, and stops polling after the final projection is fetched.

Automated acceptance:

- Dismissal opens exactly one checkout-abandonment case.
- A later `payment.failed` replaces/merges according to the declared precedence without opening a
  duplicate case.
- `payment.captured` and `order.paid` close the same case idempotently.
- All pending actions are cancelled after verified payment.
- Current-session recovered amount is correct and global history cannot alter it.
- Expired/tampered recovery tokens and already-paid orders cannot reopen Checkout.

Day 3 exit criteria:

- Scenario Lab cannot reasonably be read as a production revenue claim.
- AI evaluation demonstrates measurable value over a frozen baseline without weakening safety.
- The hero UI can be understood without verbal explanation.
- Both hero paths pass automated provider-fixture acceptance before manual rehearsal.

---

## Day 4 — Integrated hardening, provider rehearsal, and submission evidence

### Day 4 outcome

No new feature development. The day is reserved for integration fixes, two successful real test-mode
rehearsals, security/redaction checks, performance checks, documentation, and a backup recording.

### 1. Run the clean release gate

Start from a fresh disposable database/volume or a dedicated release database. Do not delete the
developer's existing persistent volume as part of an automated command.

Required sequence:

1. Build API and dashboard images.
2. Apply migrations from zero.
3. Run lint, complete Python tests, coverage floor, TypeScript check, and production build.
4. Run foundation verification twice.
5. Run deterministic simulator regression.
6. Run the frozen AI evaluation and safety gates.
7. Run API authorization, merchant-isolation, token-redaction, duplicate-provider-event, and
   out-of-order-event tests.
8. Run the full synthetic batch twice and prove replay/idempotency.
9. Verify the public dashboard contains no operator token or secret.
10. Verify `git status` contains only intentional source/documentation changes.

### 2. Perform two manual provider-verified rehearsals (C3)

Use Razorpay test mode and a fresh browser session for each path. An authorized human must enter any
required contact or test-payment details; automation must not record them.

#### Hero path A — Checkout abandonment

1. Create a fresh server-side fixed-amount order.
2. Open and dismiss Checkout.
3. Observe the bounded server-side payment-state recheck.
4. Observe one `CHECKOUT_ABANDON` case.
5. Observe AI explanation and, if the cohort demo is separate, do not imply the single case needed AI
   root-cause analysis.
6. Use `Continue recovery` for the original order.
7. Complete Razorpay test payment.
8. Confirm signed provider truth closes the same case, attributes the exact amount, cancels email,
   updates session metrics, and finishes the timeline.

#### Hero path B — Payment failure

1. Create another fresh order.
2. Cause a documented Razorpay test failure.
3. Confirm exactly one `PAYMENT_FAILURE` case and no competing abandonment case.
4. Confirm diagnosis, proposal/planning, gate, and recovery route.
5. Complete the original order successfully.
6. Confirm same-case closure and idempotent handling of replayed success/failure webhooks.

Capture sanitized acceptance artifacts using the existing capture workflow. Extend its checks to
include:

- original-order reuse;
- provider-verified payment;
- same case before and after recovery;
- pending-action cancellation;
- correct current-session recovered amount;
- no blocking provider failure;
- audit projection replay match;
- no sensitive identifiers or tokens.

### 3. Rehearse the meaningful AI incident

Run the HDFC/netbanking incident through the same aggregation path used by production observations,
clearly labelled as an incident replay if synthetic fixtures are used.

The evidence must show:

1. Observed current and baseline denominators.
2. Qualified aggregate slices sent to the model without PII.
3. Structured AI root-cause and intervention proposal.
4. Deterministic evidence/scope/action validation.
5. Scoped consequence affecting only matching cases.
6. Unrelated payment cases continuing normally.
7. Model cost, latency, schema status, and audit events.
8. Safe behavior when the model is disabled or returns an invalid proposal.

### 4. Final documentation and presentation

Update:

- `README.md` with one short setup path and one release-gate command.
- `docs/API_RELEASE_RUNBOOK.md` with the new operator boundary, attempt observations, AI proposal
  validation, and two hero rehearsals.
- A capability matrix separating live, simulated, and architecture-ready surfaces.
- A limitations section covering test-mode payments, synthetic effects, buildathon auth containment,
  and deferred provider integrations.
- A 90-second primary demo script and a backup recording.

Recommended demo order:

```text
1. Complete one live ₹500 abandonment recovery.
2. Show the same case's immutable timeline and stopped actions.
3. Replay the multi-event issuer incident.
4. Show AI proposal → deterministic validation → scoped circuit breaker.
5. Show Scenario Lab estimates, assumptions, uncertainty, and exceptions.
6. End on the architecture boundary: AI resolves ambiguity; policy controls authority.
```

Day 4 final release gate:

- Both provider-verified acceptance artifacts have `passed: true`.
- The AI incident artifact has accepted proposal, validated scope, and unaffected control cases.
- The model-disabled artifact proves safe fallback.
- `make release-gate` passes from a clean environment.
- No critical/high issue in this document remains open without an explicit limitation in the
  submission.

## Required test matrix

| Area | Required tests |
|---|---|
| Operator boundary | Missing token, invalid token, valid token, wrong merchant, object enumeration, secret absent from client |
| Session isolation | Session A/B access, expired token, tampered token, rate limits, reused token |
| Payment observations | Failure dedupe, success dedupe, `captured` + `order.paid` reconciliation, missing dimensions, live/synthetic separation |
| Cohort aggregation | Real denominators, minimum samples, baseline window, near miss, clean noise, issuer/method/gateway scopes |
| AI provider | Success, timeout, auth failure, quota, invalid JSON/schema, retry, cost ledger, PII exclusion |
| AI validator | Invented evidence, expanded scope, unsupported action, low confidence, excessive TTL, unsafe suppression |
| Circuit breaker | Exact scope, unrelated control case, duplicate proposal, expiry, pending-action cancellation |
| Recovery | Dismissal, payment failure precedence, original order, success webhook ordering, pending-action cancellation |
| Measurement | Provenance, margin sensitivity, treatment sensitivity, multiple seeds, interval calculation, replay |
| Redaction | Recipient, session/order/action/provider IDs, recovery URLs, tokens, browser attempt IDs, operator token |
| Release | Fresh migration, reused DB, full build, verifier twice, batch replay, acceptance artifact schema |

## Submission claims that are allowed after this plan

Use claims like:

- “One provider-verified Razorpay recovery loop covering payment failure and checkout abandonment.”
- “Five simulated expansion surfaces sharing the same bounded recovery spine.”
- “AI performs aggregate root-cause analysis and proposes a scoped intervention; deterministic policy
  validates every proposal before action.”
- “Synthetic treatment-vs-holdout estimates validate the measurement system; they are not production
  revenue claims.”
- “Every provider event, proposal, verdict, action, and verified outcome is replayable from an
  append-only audit trail.”

Do not claim:

- “₹28 lakh of revenue recovered” without the word `simulated estimate`.
- “Five live integrations.”
- “Perfect AI accuracy.”
- “Fully autonomous payments.”
- “Production-ready multi-tenant authentication.”
- “Prompt-injection-proof” when the tested boundary is structured aggregate input.

## Final positioning

The final product story should be:

> Leakproof is a safety-first revenue-recovery agent. It detects payment leakage, uses AI where
> multi-event evidence is genuinely ambiguous, validates every proposal against deterministic
> policy, executes only bounded recovery actions, and measures incremental impact with explicit
> assumptions and provenance. AI resolves ambiguity; policy controls authority; Razorpay supplies
> payment truth.
