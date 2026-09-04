# Leakproof

**One live provider-verified recovery loop; five simulated expansion surfaces.**

This repository contains the August 25 foundation through the September 4 reproducible full-batch slice.
A FastAPI receiver verifies Razorpay HMAC signatures, commits each raw webhook
to a durable Postgres inbox, deduplicates by provider event ID, and only then asks a Celery worker
to normalize it. A fixed-seed simulator sends all four revenue-leak types through that same case
and append-only event spine. PostgreSQL rejects updates and deletes against the event timeline,
worker redelivery cannot append duplicate events or repeat an actuator call, and repeating a
simulator seed cannot duplicate cases. A ten-minute aggregate cohort scan uses durable,
deduplicated success and failure observations to detect qualified issuer incidents without
sending customer or entity identifiers to the model.

## Capability and provenance contract

These API and dashboard labels are not interchangeable.

| Label | What is shipped | What it does not claim |
|---|---|---|
| `LIVE_PROVIDER_VERIFIED` | One Razorpay test-mode recovery loop covering payment failure and checkout abandonment, closed only by a signed Checkout result plus captured-payment API verification or a signed success webhook | Live invoice, subscription, voice, or autonomous money movement |
| `SIMULATED_END_TO_END` | Scenario Lab coverage for payment failure, checkout abandonment, invoice overdue, and subscription halt | Realized merchant revenue or provider-verified recovery |
| `ARCHITECTURE_READY` | Bounded voice/promise and provider-adapter boundaries without a connected live provider | A live customer-contact integration |

`GET /capabilities` publishes the same safe matrix. Scenario Lab rejects anything other than
`SIMULATED_END_TO_END`; Live Demo rejects anything other than `LIVE_PROVIDER_VERIFIED`.

## Declared limitations

- Razorpay evidence is test-mode integration evidence, not production payment volume or realized
  recovery lift.
- Scenario Lab financial output is a simulated estimate derived from declared treatment effects,
  contribution margin, costs, exclusions, seeds, and uncertainty intervals.
- The operator bearer credential is buildathon containment. Production requires merchant identity,
  OAuth/RBAC, rotation, and an authenticated operator surface.
- Invoice, subscription, voice, Resend recipient delivery, and additional provider adapters
  remain simulated, preview-only, or architecture-ready exactly as labelled above.

The post-release plan for turning checkout abandonment, invoice overdue, and subscription halt
into evidence-gated Razorpay test-mode demos is documented in
[`MULTI_SURFACE_RECOVERY_IMPLEMENTATION_PLAN.md`](MULTI_SURFACE_RECOVERY_IMPLEMENTATION_PLAN.md).

```text
Razorpay webhook                                  Fixed-seed merchant simulator
      │ HMAC first                                5,000 customers · 12 months
      ▼                                           5 leak types · 5 scenarios
FastAPI ──commit──> Postgres webhook inbox                    │
                              │                               │
                         Celery worker                        │
                              └──────────┬────────────────────┘
                                         ▼
                              Case projection + events
                                  │              │
                          replay endpoint   aggregate cohort scan
                                                  │ structured output + ledger
                                                  ▼
                                      scoped circuit breaker
```

## September 4 headline scoreboard

The committed seed-42 simulation now runs all 687 cases through diagnosis, holdout assignment,
bounded planning, pre-flight gating, deterministic actuators, outcome verification, and the
exception ledger. These are synthetic measurements from the assumptions in
`simulator/params.yaml`, not production revenue claims.

| Measure | Seed-42 result |
|---|---:|
| Treatment recovery rate | 22.61% (161 / 712) |
| Holdout recovery rate | 14.67% (11 / 75) |
| Simulated treatment-vs-holdout lift estimate | **+7.95 pp (single seed)** |
| Simulated gross recovery | ₹99,26,100.51 |
| Stratified organic counterfactual | ₹71,05,521.53 |
| Incremental recovery | **₹28,20,578.98** |
| Intervention + model cost | ₹31.22 |
| Simulated net economic-value estimate | **Calculated from incremental revenue × declared contribution margin, less declared costs** |
| False chases | **0** |
| Scoped issuer-outage suppressions | **47 / 47** |
| Prompt-injection bypasses | **0 / 64** |

The result is deterministic across clean databases: outcome draws use the committed case dedupe
key, action type, step number, and seed rather than generated database IDs. The amount estimator is
the pre-declared stratified holdout amount-rate estimator. The complete non-recovered population is
published through the exception endpoint and grouped on the dashboard; no case is dropped from the
report.

Synthetic identities include a simulator schema version. A newer build therefore creates a fresh,
auditable run namespace instead of silently inheriting mutable projections from an older seeded
volume.

An immutable seed-42 circuit-breaker audit export is committed at
`samples/seed-42-audit.json` for review without a running stack.

## Quick setup and final release

Docker Desktop must be running.

This release path is sandbox-only. It requires Razorpay Test Mode keys (`rzp_test_…`) but does not
require Live Mode activation, KYC, a settlement bank account, or real payment details. The
application rejects `rzp_live_…` keys.

```bash
cp .env.example .env
# Set LEAKPROOF_OPERATOR_API_TOKEN to: openssl rand -base64 32
make up
curl http://localhost:8000/health/ready
```

After the two human-authorized Razorpay test-mode rehearsals have produced the sanitized artifacts
described in the release runbook, the single final submission command is:

```bash
make release-gate
```

For historical checkpoint verification, the individual slices remain available:

```bash
make demo-webhook
make verify-foundation
make seed
make test-august-27
make test-august-28
make test-august-29
make test-api-august-29
make test-api-august-30
make test-api-august-31
make test-api-september-1
make test-api-september-2
make test-api-september-3
make test-august-30
make test-august-31
make test-september-1
make test-september-2
make test-september-3
make test-september-4
make batch
make evals
```

The `make up` path builds one `leakproof-app:latest` application image, starts Postgres and Redis,
applies the foundation schema and append-only enforcement migrations, then starts the API, Celery
worker, and Celery Beat. The migration job and all three application services reuse the same image;
Postgres and Redis remain separate infrastructure containers. The Next.js dashboard is available
at `http://localhost:3000` and reads the same API data used by the acceptance tests. Beat also rescans the durable inbox
every minute, so a webhook committed during a temporary broker outage is not lost.

The operational API (`/cases`, scoreboards, evaluations, costs, suppressions, batch execution,
voice turns, replay, and detailed audit) requires the server-side operator bearer credential and
derives merchant scope from `LEAKPROOF_OPERATOR_MERCHANT_IDS`. Out-of-scope object reads return
`404`; scoped collections omit other merchants. The public dashboard keeps Case Timeline disabled
unless `LEAKPROOF_OPERATOR_UI_ENABLED=true` on a separately protected operator surface. The token
is attached only by Next.js server code and is never returned to the browser. Production must
replace this buildathon boundary with merchant identity plus OAuth/RBAC.

The public recovery API is implemented through the 4 September checkpoint. A failed test payment
can be confirmed by the server through the original order's Razorpay payment API even when no
Dashboard webhook is configured; signed failure webhooks remain a complementary reconciliation
path. A Checkout success closes a case only after the server recomputes Razorpay's HMAC using the
server-owned order ID, fetches the payment, and confirms `captured`, matching order, amount, and
currency. Signed success webhooks converge on the same idempotent close path. Recovery
links use signed 30-minute tokens bound to session, merchant, order, amount, and currency; the
bootstrap route rechecks Razorpay payment state and reuses only the original unpaid order. Run the
Razorpay checkpoint with `make test-api-august-31`.
Uvicorn access logging replaces the signed capability segment with `[REDACTED]` before rendering a
recovery request line.

Live cases now receive an asynchronous `gpt-5.6-luna` explanation after deterministic Tier 1
diagnosis. Only the failure class, payment method, amount band, and allowlisted provider
classifications enter the Responses API request. Strict `CaseInsight` output, request metadata,
usage, latency, retries, schema status, and paise cost are persisted across the model ledger,
provider-call audit, session projection, and append-only timeline. Timeout, quota, invalid-schema,
missing-configuration, and per-case budget failures produce deterministic guidance and never block
the signed recovery path. Run this checkpoint with `make test-api-september-1`.

The 2 September checkpoint adds the live-only Resend ladder. Each detected demo case gets one
registered recovery email action 7 demo seconds after its immediate in-app link. Addresses are
encrypted at rest and decrypted only immediately before sending; missing or non-allowlisted
recipients, the rolling five-per-address limit, and daily/monthly free-tier ceilings all fall back
to a safe preview without a provider call. Resend requests use the action idempotency key, while
raw-body Svix verification, durable event deduplication, and event-time reconciliation make sent,
delivered, clicked, bounced, complained, and failed webhooks converge even when they arrive out of
order. Session projections expose delivery status without recipient data. Run this checkpoint with
`make test-api-september-2`.

The 3 September checkpoint makes **Live Demo** the default dashboard. It resumes the browser's
active demo session and polls its sanitized API projection every two seconds only while the session
is active. The view separates Browser Telemetry, Razorpay Webhook, Luna, and Resend sources and
shows the deterministic diagnosis, optional insight or fallback, pre-flight gate, bounded recovery
actions, provider receipts, verified recovered amount, latency, failures, and Luna cost. The former
treatment-versus-holdout dashboard now lives at `/scenario-lab` behind an explicit synthetic-only
banner; no simulator fields enter the live projection. Run the integrated API and production UI
checkpoint with `make test-api-september-3`.

The 4 September checkpoint adds a token-protected, sanitized acceptance export and a repeatable
capture command for the two release rehearsals. The export contains blocking/advisory checks, the
safe event timeline, safe provider status, and final operational metrics while omitting recipient,
session/order/action/provider identifiers, browser attempt IDs, signed links, and tokens. It also
adds release kill switches for new sessions, Luna, and outbound email; disabling enrichments keeps
deterministic diagnosis and the customer-authorized recovery route available. Run the complete API
checkpoint with `make test-api-september-4` and follow
[`docs/API_RELEASE_RUNBOOK.md`](docs/API_RELEASE_RUNBOOK.md) for deployment, provider registration,
credential ownership, two-path rehearsal, evidence capture, known exceptions, and rollback.

`make verify-foundation` runs an end-to-end check against the API, Celery worker, and PostgreSQL. It
waits independently for three distinct processed inbox rows, verifies the semantic sequence
`DETECTED → ASSIGNED → SIGNAL → SIGNAL` on one replayable case, confirms the applied migration, and
proves PostgreSQL rejects both updates and deletes against its audit timeline. Timeout output
includes inbox/processed counts, event kinds, attempts, and redacted error summaries.

`make release-gate` is the single fail-fast submission command. It builds the API and dashboard
images, migrates a disposable PostgreSQL database from zero and reuses it once, runs lint and the
full Python suite with an 85% coverage floor, typechecks and builds the dashboard, scans public
assets for configured secret values, verifies the foundation twice, proves full-batch replay
idempotency, runs the frozen AI/safety gates, captures the scoped incident and model-disabled
evidence, and validates two live provider-rehearsal artifacts for schema, completeness, provenance,
and redaction. `make release-gate-automated` runs the repository-controlled portion before the
human provider rehearsals. A backup recording is optional; `make demo-recording-check` validates it
when one is produced.

## August 26: synthetic merchant simulator

`make seed` reads every simulator assumption from `simulator/params.yaml`, uses committed seed
`42`, creates a clearly synthetic merchant in PostgreSQL, and writes a reproducible dataset to
`artifacts/simulator/seed-42.json`. Running the command again reuses the same 5,000 customer
profiles and 687 cases without appending duplicate events.

The default run produces:

| Measure | Count |
|---|---:|
| Synthetic customers | 5,000 |
| Months of customer history | 12 |
| Historical orders | 84,020 |
| B2B invoice customers | 400 |
| At-risk cases | 687 |
| Payment failures | 327 |
| Overdue invoices | 160 |
| Abandoned checkouts | 100 |
| Halted subscriptions | 100 |
| Ground-truth organic recoveries | 236 |

The five injected scenarios are a 40-minute HDFC/netbanking issuer outage with 47 failures, a
180-customer expired-card cohort, 40 failures during a six-hour merchant misconfiguration, 60
insufficient-funds failures clustered on month-end paydays, and 160 overdue invoices drawn from
400 synthetic B2B payers with distinct late-payment histories.

Organic recovery, intervention effects, fatigue, and opt-out probabilities are **synthetic ground
truth assumptions**, not observed or attributed revenue. Future workflow and scoreboard slices will
use these values to measure treatment against holdout; this slice does not claim realized lift.
All monetary values, including synthetic orders and invoices, are stored in paise.

To inspect a different reproducible dataset without touching PostgreSQL:

```bash
uv run python scripts/seed_simulator.py --seed 43 --no-persist
```

## August 27: diagnosis, gate, and registered templates

Tier 1 diagnosis is an ordered, deterministic classifier loaded from
`config/tier1_rules.yaml`; first match wins and the stable rule ID is persisted with a `DIAGNOSED`
event. Invoice cases use the parallel `config/receivable_rules.yaml` matrix over aging bucket,
payer history, and invoice size. Unknown payment shapes fail safely into `T1_FALLBACK` for later
cohort-level analysis.

Every planned external action must receive an immutable gate verdict. The gate evaluates and
records all seven stopping rules—including passes—then checks consent, cross-case frequency caps,
channel cooldowns, 08:00–19:00 IST contact hours, retry and debit ceilings, e-mandate constraints,
two-key approval, registered-message integrity, tone, and third-party contact. A `DENY` outranks a
human deferral; quiet-hours `RESCHEDULE` is returned only when no rule denies the action. Gate
results can be written both to `actions.verdict_rules` and the append-only case timeline.

`TemplateRegistry` is the only normal constructor for `RenderedMessage`, and messaging actuators
must accept that type rather than raw strings. Unknown templates, missing variables, undeclared
variables, and unregistered languages are rejected before gating, so neither an LLM nor any other
caller has a free-text path to WhatsApp or SMS.

Run the August 27 acceptance slice alone:

```bash
make test-august-27
```

## August 28: fixed-prior EV policy and bounded planner

The policy scores configured actions deterministically:

```text
EV = P(recover | class, segment, action) × amount_at_risk × margin
     − direct_cost
     − annoyance_lambda × intrusiveness × amount_at_risk
```

The default margin is `1.0` because Leakproof recovers revenue rather than estimating profit. Both
margin and the default `0.02` annoyance coefficient can be overridden by merchant policy. Recovery
probabilities use fixed beta-prior means from `config/priors.yaml`, with segment-specific cells
taking precedence over class cells and the global fallback. No Thompson sampling or fabricated
observations are used; cells below 30 prior observations are marked exploratory in every step.

`config/ladders.yaml` declares bounded, cheapest-first escalation ladders. Planner startup rejects
unknown actions, duplicate class mappings, overlong ladders, or a ladder that moves backward to a
cheaper/less intrusive action. Static eligibility removes class-inapplicable actions, prohibited
customer contact, protected-case automation, contact-budget violations, and consent-dependent
channels without recorded consent before EV selection.

Retry timing is diagnosis-specific: `TRANSIENT` uses bounded 6/24/72-hour backoff, `TIMING` targets
the next first-of-month or last working-day payday, `FRICTION` retries after 30 minutes, and
`INSTRUMENT_DEAD` never retries. Every leak type renders a positive-EV plan when one exists. If no
registered ladder or eligible positive-EV action exists, the case closes as `ABANDONED` with every
rejected candidate and reason stored in its append-only `CLOSED` event. Successful plans store the
same explanation in a `PLANNED` event. Replanning is idempotent.

Run the August 28 acceptance slice alone:

```bash
make test-august-28
```

## August 29: durable schedules and idempotent simulation actuators

Successful plans now materialize one `actions` row per bounded step. Celery Beat scans due
Postgres schedules every 30 seconds and sends their IDs to late-ack workers. The worker locks the
action, re-evaluates the complete guardrail gate using current consent, contact, suppression, and
case state, then dispatches through the adapter selected for the action. Quiet-hours verdicts move
the same schedule to the next contact window; denied or human-deferred work never reaches an
actuator.

Simulation payment, registered-message, voice, and human-queue adapters share the production
contract. An actuator can only accept an immutable `GateVerdict`, and every customer-facing
simulation call receives a `RenderedMessage` from the registry. Deterministic keys are derived
from case, step, and attempt; a durable simulated-provider receipt ledger deduplicates the external
side as well as the worker side. Re-delivering the same Celery task therefore produces one receipt
and one `ACTED` event.

`payment.captured` and `order.paid` webhooks now match the original case by payment/order identity,
append verification and recovery events, close the case, and atomically cancel all unexecuted
steps. A repeated success webhook cannot duplicate those terminal events.

Run the August 29 acceptance slice alone:

```bash
make test-august-29
```

## August 30: aggregate Tier 2 reasoner and circuit breaker

Celery Beat scans twenty-minute payment-attempt windows every ten minutes. Verified
`payment.failed`, `payment.captured`, and `order.paid` webhooks are reconciled into a sanitized
`payment_attempt_observations` ledger; a captured payment plus its `order.paid` event counts once.
The input contract contains only observed counts, issuer/method or other safe aggregate dimensions,
stable evidence-slice IDs, and reason frequencies. It never derives a denominator from a declared
failure-rate field. Before model routing, a deterministic qualifier requires at least 20 current
attempts, 50 historical attempts, a failure rate at least three times baseline, and an 80%
bank-or-gateway error share. Missing evidence returns `INSUFFICIENT_DATA` without calling a model.

Qualified aggregates use a versioned prompt and a strict structured-output schema. In `live_demo`,
the factory selects the tool-free, non-stored OpenAI Responses adapter or an explicitly unavailable
provider that degrades safely; it never substitutes deterministic output. Every logical call,
including failures and budget denials, is written to `llm_calls` with provider request ID, error
class, model, prompt version, tokens, paise cost, latency, schema status, and retry count. Simulation
and offline evaluation alone use the deterministic transport.

Model output is only a proposal. Deterministic validation rechecks evidence-slice IDs, observed
thresholds, scope containment, action, confidence, and a 120-minute TTL ceiling before any
consequence. `AI_PROPOSED`, `POLICY_VALIDATED` or `AI_PROPOSAL_REJECTED`, and the resulting
`SUPPRESSION_OPENED`, `RETRY_DELAYED`, `MERCHANT_ALERTED`, or `NO_ACTION` events remain distinct in
the timeline. An accepted `GLOBAL_SUPPRESS` proposal opens one idempotent, scoped circuit breaker;
all 47 injected HDFC/netbanking cases are suppressed while unrelated cases remain actionable.

Run the August 30 acceptance slice alone:

```bash
make test-august-30
```

## August 31: attribution, stratified holdout, and scoreboard

Measurement rules are fixed in `config/measurement.yaml` before a run: a committed seed assigns
10% of cases to holdout within `(leak_type, amount_band)` strata, payment and checkout windows are
7 days, subscription and mandate windows are 14 days, and invoice windows are 21 days. Every new
case receives an append-only `ASSIGNED` event containing the seed, stratum, bucket, threshold, and
arm. Holdout cases still pass through diagnosis and audit logging, but planning creates no action
rows and the actuator has a second enforcement check; they therefore create no `contacts` rows and
consume no frequency budget.

Paid signals match by entity identity or by customer plus an amount within 1%. Eligible treatment
payments receive credit for the most recent executed action; holdout recoveries and treatment
recoveries with no prior action are labelled organic. A payment outside the pre-declared window
still closes the case and cancels pending work, but creates no attribution row, so the scoreboard
cannot claim it as recovered revenue. Each successful intervention moves the window forward from
that touch.

`GET /scoreboard/{run_id}` separates gross recovered revenue, incremental revenue, contribution
margin, and net economic value. It includes intervention, model, human-review, and declared optional
costs plus the estimator, assumption hash, seed count, strict assumptions, and uncertainty object.
The single-run API declares point uncertainty honestly; `scripts/run_sensitivity.py` uses an
isolated temporary database to report empirical median, min/max, and 80% intervals across several
seeds and exactly three treatment-effect multipliers. No live table is touched; the committed
five-seed output is [`samples/day3-sensitivity.json`](samples/day3-sensitivity.json).

Run the August 31 acceptance slice alone:

```bash
make test-august-31
```

## September 1: cohort and adversarial evaluation harness

`make evals` runs three reproducible, committed corpora. The generated cohort suite is retained as
`simulator_regression`, not described as generalization. It contains 120 aggregate
windows: 40 labeled anomalies across all five supported patterns, 50 threshold near-misses, and
30 clean windows. It reports precision, recall, and F1 per pattern and overall, and fails when F1
regresses by more than two percentage points from the last passing persisted run or when the
false-suppression rate exceeds 5%. The default offline transport is deterministic, so this gate is
free and repeatable; it evaluates the simulation transport, not the quality of a hosted model.

The separate `decision_quality` suite is a frozen, manually authored set with separately reviewed
AI proposal captures. It reports rules-only, raw-AI, and AI-plus-deterministic-validator results for
root-cause F1, exact scope, recommendation quality, unsupported evidence, false suppression, schema
validity, safe fallback, cost, and latency. Safety gates are strict; quality must exceed the recorded
rules baseline by the declared margin.

The injection suite contains 64 untrusted-text payloads across instruction overrides, fake system
messages, tool abuse, guardrail bypasses, exfiltration requests, multilingual attacks, encoded
variants, and benign lookalikes. Every payload is checked against a stripped-input control: the
gate verdict and state must remain identical, the action must come from `config/actions.yaml`, the
message must be a `TemplateRegistry` product, and model output must contain no PII. Any bypass
fails the run. Benign lookalikes must continue to produce the clean `ALLOW` result.

Results are retained in `eval_runs`, written to `evals/report.json`, and exposed through
`GET /evals/latest`. GitHub Actions runs lint, the full tests, and the no-database evaluation gate
on every push and pull request. Run this slice alone with:

```bash
make test-september-1
make evals
```

## September 2: recordable scoreboard and case timeline

The Next.js dashboard ships the two screens on the demo critical path. The Scoreboard reads the
latest measured batch and separates simulated gross recovery from the stratified holdout
counterfactual, incremental revenue, contribution margin, declared costs, and simulated net
economic-value estimate. It also shows assumptions, excluded costs, throughput, the
five leak surfaces, false chases, circuit-breaker suppressions, EV declines, human escalations,
unresolved exceptions, and the latest retained evaluation status. Synthetic runs are labelled on
screen rather than presented as production outcomes.

The Case Timeline uses the append-only audit spine directly. A filterable case index opens the
assignment, deterministic diagnosis and rule ID, bounded ladder, complete gate results, executed
or denied actions, provider reference, verification, and final outcome in order. The projection is
replayed and checked on every detail request, and the same record can be downloaded as `audit.json`.
There is no mock dashboard dataset: an unavailable API or missing seed produces an explicit empty
state.

`make up` serves the dashboard with the rest of the stack. For frontend-only development, keep the
API on port 8000 and run `make dashboard`. Run the September 2 acceptance slice with:

```bash
make test-september-2
```

## September 3: bounded Hinglish voice and promise-to-pay

The simulation voice adapter now continues into a deterministic, maximum-two-customer-turn
dialogue for overdue invoices. A turn is accepted only after a `voice_hinglish` action has passed
the normal consent, contact-window, frequency, suppression, and two-key checks and executed
successfully. Customer transcripts are treated as untrusted input: they can select only a small
intent set, while every spoken response comes from the registered template registry. The workflow
can confirm identity, provide the existing secure payment link, capture a full-outstanding
promise date within 30 days, stop, or hand off. It cannot negotiate, settle, threaten, alter an
amount, or create free-form speech.

Each provider turn has a durable idempotency key and is copied into the append-only case timeline.
A captured promise is stored once with its transcript reference and is returned in case detail.
English, Hindi, and common Hinglish stop phrases end the call immediately; explicit do-not-call
language records the opt-out and cancels pending contact. Live ASR, TTS, and telephony remain an
explicit integration boundary—the shipped path is a reproducible simulator, not a claim of a live
voice deployment.

Run the September 3 acceptance slice with:

```bash
make test-september-3
```

## September 4: reproducible full batch and honest exception list

`make batch` executes the complete seed-42 dataset through the same case, event, diagnosis, policy,
gate, actuator, attribution, and scoreboard code paths used by the API. The issuer outage is scanned
before individual recovery work and opens one HDFC/netbanking circuit breaker. Outcome draws are
fixed by stable business keys, organic and intervention recoveries are materialized as normal paid
signals, and exhausted attribution windows close explicitly as `LOST`. Re-running the batch adds no
events, actions, contacts, attributions, suppressions, or provider receipts.

`POST /batch/run` provides the same synchronous simulation entry point. The scoreboard attributes
aggregate Tier 2 model cost to its batch, so net value includes both per-case intervention costs and
cohort reasoning. `GET /costs?run_id=...` exposes the corresponding ledger slice.

The dashboard now includes a real exception table grouped by reason. The backing
`GET /scoreboard/{run_id}/exceptions` response includes every individual non-recovered case ID plus
its leak type, state, outcome, amount at risk, and reason. Seed 42 publishes these groups:

| Exception reason | Cases | Interpretation |
|---|---:|---|
| Recovery not observed | 435 | No verified recovery inside the declared window |
| Human review | 95 | Protected, high-value, or sensitive action needs a second key |
| Merchant remediation | 39 | Merchant configuration must be fixed first |
| Cohort suppression | 24 | Still non-recovered after safe suppression; all 47 were protected |
| Protected customer | 10 | Automation prohibited; human-only policy |
| Contact prohibited | 7 | Existing DNC status blocked intervention |
| Customer opt-out | 5 | Opt-out recorded and later contact cancelled |

Run the September 4 acceptance slice with:

```bash
make test-september-4
make batch
```

Run the acceptance tests locally:

```bash
make install
make test
```

## Live API integration: 30 August checkpoint

`POST /demo/sessions` now creates a fixed ₹500 Razorpay test order through the provider-neutral
payment boundary and returns only the public Checkout key, original order ID, opaque session ID,
signed session token, amount, currency, expiry, and email mode. The browser cannot choose the
amount or currency. Allowlisted reviewer addresses are encrypted at rest; other recipients stay
in `preview_only` mode and are never retained in plaintext.

Checkout telemetry accepts only the four frozen event types, binds every request to the signed
session token, deduplicates by browser event ID, and enforces both rolling Redis limits and a
database cap. A dismissal schedules a configurable server-side re-check (seven seconds in the
public demo). The worker re-reads later browser
events and Razorpay payment state before creating one treatment-only `CHECKOUT_ABANDON` case. A
15-second Beat scan rescues a persisted dismissal if the immediate broker handoff fails.

The dashboard defaults to the live operational view at `http://localhost:3000`. The browser flow
at `http://localhost:3000/demo` creates the fixed order,
loads Razorpay Checkout, persists idempotent telemetry before delivery, and handles submission,
failure, and dismissal without treating browser callbacks as payment truth. Signed links open at
`/recover/{token}` and reuse the verified original order rather than creating a replacement order.

Run this API checkpoint with:

```bash
make test-api-august-30
```

The tests demonstrate:

- invalid signatures are rejected before persistence;
- replaying one provider event stores one inbox row;
- webhooks without a provider event ID deduplicate using a stable fingerprint;
- three payment failures for one customer/order create one case, one assignment, and three signal events;
- worker redelivery does not append another signal or assignment event;
- broker outages leave a committed webhook available for the scheduled inbox dispatcher;
- the case projection can be rebuilt from its append-only timeline;
- the replay API returns the complete ordered timeline and unknown cases return HTTP 404;
- event timeline updates and deletes are rejected;
- all YAML configuration loads into typed models;
- the simulator creates 5,000 customer profiles with 12 months of reproducible history;
- every leak type and every required incident scenario is present;
- organic recoveries are represented for all four leak types;
- reseeding does not duplicate customers, cases, or append-only events;
- every leak type renders a bounded positive-EV plan or an explained `ABANDONED` result;
- fixed-prior decisions, retry timing, and cheapest-first ladder constraints are deterministic;
- successful plans persist durable due-action schedules;
- duplicate worker delivery creates one simulated provider receipt and one `ACTED` event;
- paid webhooks close the case and cancel every later pending action.
- aggregate Tier 2 input contains no customer or entity identifiers;
- the issuer outage opens one breaker and suppresses exactly 47/47 matching cases;
- malformed structured output is cost-audited, records `AI_DEGRADED → NO_ACTION`, and suppresses zero cases;
- repeated cohort scans do not duplicate breakers or suppression events.
- holdout assignment is reproducible, stratified, and present in the audit timeline;
- holdout cases are diagnosed without schedules, interventions, contacts, or frequency usage;
- attribution windows vary by leak type and advance from the latest successful touch;
- last-touch and organic payment credit are persisted without claiming late payments;
- the scoreboard computes holdout lift, incremental recovery, costs, net value, and exceptions.
- the latest batch scoreboard includes the case mix required by the dashboard;
- filtered case reads expose diagnosis, plans, attribution, and ordered audit events;
- the recordable Scoreboard and Case Timeline compile as a production Next.js application.
- bounded voice accepts only successfully gated invoice calls and registered reply templates;
- provider turn redelivery creates one voice turn and one promise-to-pay record;
- opt-out language ends the call immediately, records DNC, and cancels later contact;
- unclear voice input hands off after at most two customer turns.
- the seed-42 full batch is terminal, reproducible, measured, and idempotent;
- one scoped issuer breaker suppresses all 47 injected outage cases before action;
- every non-recovered case appears in the grouped and case-level exception report;
- aggregate model cost is attributed to the batch and included in net value.

## API

- `POST /demo/sessions` — fixed Razorpay test order and signed Checkout session bootstrap
- `POST /demo/sessions/{session_id}/checkout-events` — bounded, idempotent browser telemetry
- `POST /demo/sessions/{session_id}/payments/verify` — token-bound Checkout HMAC and captured-payment API verification
- `GET /demo/sessions/{session_id}` — sanitized live case, action, provider, timeline, and metrics projection
- `GET /demo/sessions/{session_id}/acceptance.json` — token-protected sanitized release evidence and acceptance checks
- `GET /recover/{signed_token}` — verified bootstrap for the original unpaid Razorpay order
- `POST /webhooks/razorpay` — HMAC verification, durable inbox, dedupe, async enqueue
- `POST /webhooks/resend` — raw-body signature verification and redacted delivery reconciliation
- `GET /capabilities` — public capability/provenance matrix without merchant data
- `POST /batch/run` — operator-only execution or replay of the synthetic batch
- `GET /cases?state=&leak_type=` — operator-only, merchant-scoped case index
- `GET /cases/{case_id}` — operator-only case detail and audit timeline
- `POST /actions/{action_id}/voice/turns` — operator-only bounded simulated voice turn
- `GET /cases/{case_id}/audit.json` — operator-only append-only audit export
- `GET /cases/{case_id}/replay` — operator-only projection-integrity replay
- `GET /suppressions` — operator-only, merchant-scoped circuit breakers
- `POST /suppressions/{id}/close` — operator-only circuit-breaker override
- `GET /costs` — operator-only, merchant-scoped model cost rollup
- `GET /scoreboard/{run_id}` — operator-only, merchant-scoped synthetic metrics
- `GET /scoreboard/latest` — operator-only latest permitted synthetic batch
- `GET /scoreboard/{run_id}/exceptions` — operator-only batch exceptions
- `GET /evals/latest` — operator-only evaluation metrics with pass/fail gates
- `GET /health/live` and `GET /health/ready` — process and database health

Raw money values are paise (`BIGINT`) and timestamps are timezone-aware. Actuator calls and the
structured cohort transport are deterministic simulations in the default mode.

## Current boundary

The approved free-first live integration direction is documented in
[`API_INTEGRATION_PLAN.md`](API_INTEGRATION_PLAN.md).

The simulator feeds normalized synthetic signals directly into the shared case/event spine and
remains visibly isolated under Scenario Lab. Razorpay test order creation, browser Checkout
telemetry, signed original-order recovery, Razorpay success/failure reconciliation, Luna case
insights with deterministic fallback, and allowlisted Resend delivery are implemented for the
public live-demo path. The scheduled invoice, subscription, and 24-hour reconciliation entry
points remain simulator-backed rather than public live integrations.
Attribution, holdout enforcement, the scoreboard API, the reproducible September 1 evaluation
harness, the September 2 Scoreboard and Case Timeline, the September 3 bounded simulated voice,
and the September 4 full-batch outcome and exception workflow are built. Live voice transport is
not.
