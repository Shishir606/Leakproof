# Leakproof

**One recovery spine for five revenue-leak surfaces.**

This repository contains the August 25 foundation through the September 4 reproducible full-batch slice.
A FastAPI receiver verifies Razorpay HMAC signatures, commits each raw webhook
to a durable Postgres inbox, deduplicates by provider event ID, and only then asks a Celery worker
to normalize it. A fixed-seed simulator sends all five revenue-leak types through that same case
and append-only event spine. PostgreSQL rejects updates and deletes against the event timeline,
worker redelivery cannot append duplicate events or repeat an actuator call, and repeating a
simulator seed cannot duplicate cases. A ten-minute aggregate cohort scan detects qualified
issuer incidents without sending customer or entity identifiers to the model.

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

The committed seed-42 simulation now runs all 787 cases through diagnosis, holdout assignment,
bounded planning, pre-flight gating, deterministic actuators, outcome verification, and the
exception ledger. These are synthetic measurements from the assumptions in
`simulator/params.yaml`, not production revenue claims.

| Measure | Seed-42 result |
|---|---:|
| Treatment recovery rate | 22.61% (161 / 712) |
| Holdout recovery rate | 14.67% (11 / 75) |
| Measured lift | **+7.95 pp** |
| Gross treatment recovery | ₹99,26,100.51 |
| Stratified organic counterfactual | ₹71,05,521.53 |
| Incremental recovery | **₹28,20,578.98** |
| Intervention + model cost | ₹31.22 |
| Net value created | **₹28,20,547.76** |
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

## Run the August 25–September 4 slices

Docker Desktop must be running.

```bash
cp .env.example .env
make up
curl http://localhost:8000/health/ready
make demo-webhook
make verify-foundation
make seed
make test-august-27
make test-august-28
make test-august-29
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

`make verify-foundation` runs a fresh end-to-end check against the live API, Celery worker, and
PostgreSQL. It sends three payment failures plus a duplicate, verifies that all three signals land
on one replayable case, confirms the applied migration, and proves PostgreSQL rejects both updates
and deletes against its audit timeline.

## August 26: synthetic merchant simulator

`make seed` reads every simulator assumption from `simulator/params.yaml`, uses committed seed
`42`, creates a clearly synthetic merchant in PostgreSQL, and writes a reproducible dataset to
`artifacts/simulator/seed-42.json`. Running the command again reuses the same 5,000 customer
profiles and 787 cases without appending duplicate events.

The default run produces:

| Measure | Count |
|---|---:|
| Synthetic customers | 5,000 |
| Months of customer history | 12 |
| Historical orders | 84,020 |
| B2B invoice customers | 400 |
| At-risk cases | 787 |
| Payment failures | 327 |
| Overdue invoices | 160 |
| Abandoned checkouts | 100 |
| Halted subscriptions | 100 |
| Broken mandates | 100 |
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

Celery Beat now scans twenty-minute failure windows every ten minutes. The input contract contains
only counts, baselines, issuer/method dimensions, and reason frequencies. Before model routing, a
deterministic qualifier requires at least 20 attempts, a failure rate at least three times baseline,
and an 80% bank-or-gateway error share. This keeps ordinary and near-miss traffic out of Tier 2.

Qualified aggregates use a versioned prompt and a strict structured-output schema. Schema failures
retry once through the configured escalation route, then degrade to no anomaly without stopping the
batch. Every logical call, including failures and budget denials, is written to `llm_calls` with
model, prompt version, tokens, paise cost, latency, schema status, and retry count. Simulation uses a
deterministic structured transport; a production transport can implement the same client contract.

A `GLOBAL_SUPPRESS` result with confidence at least 0.80 opens one idempotent, scoped circuit
breaker. Matching cases move to `SUPPRESSED`, pending actions are cancelled, and all 47 injected
HDFC/netbanking outage cases receive an append-only `SUPPRESSED` event. Unrelated merchant cases
remain actionable, and actuator pre-flight checks now match the case evidence against the open
scope instead of treating every merchant suppression as global.

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

`GET /scoreboard/{run_id}` reports treated and holdout recovery rates, percentage-point lift,
gross and holdout recovery, a stratum-weighted organic counterfactual, incremental recovery,
intervention and model costs, net value, contact efficiency, opt-outs, false chases, suppressions,
human escalations, EV declines, and unresolved exceptions. Money is returned in paise and the
estimator name is included in the response.

Run the August 31 acceptance slice alone:

```bash
make test-august-31
```

## September 1: cohort and adversarial evaluation harness

`make evals` runs two reproducible, committed corpora. The cohort suite contains 120 aggregate
windows: 40 labeled anomalies across all five supported patterns, 50 threshold near-misses, and
30 clean windows. It reports precision, recall, and F1 per pattern and overall, and fails when F1
regresses by more than two percentage points from the last passing persisted run or when the
false-suppression rate exceeds 5%. The default offline transport is deterministic, so this gate is
free and repeatable; it evaluates the simulation transport, not the quality of a hosted model.

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
latest measured batch and separates gross recovery from the stratified holdout counterfactual,
incremental recovery, intervention and model costs, and net value. It also shows throughput, the
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
database cap. A dismissal schedules a 30-second Celery re-check. The worker re-reads later browser
events and Razorpay payment state before creating one treatment-only `CHECKOUT_ABANDON` case. A
15-second Beat scan rescues a persisted dismissal if the immediate broker handoff fails.

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
- organic recoveries are represented for all five leak types;
- reseeding does not duplicate customers, cases, or append-only events;
- every leak type renders a bounded positive-EV plan or an explained `ABANDONED` result;
- fixed-prior decisions, retry timing, and cheapest-first ladder constraints are deterministic;
- successful plans persist durable due-action schedules;
- duplicate worker delivery creates one simulated provider receipt and one `ACTED` event;
- paid webhooks close the case and cancel every later pending action.
- aggregate Tier 2 input contains no customer or entity identifiers;
- the issuer outage opens one breaker and suppresses exactly 47/47 matching cases;
- malformed structured output retries once, is logged, and suppresses zero cases;
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
- `POST /webhooks/razorpay` — HMAC verification, durable inbox, dedupe, async enqueue
- `POST /batch/run` — execute or idempotently replay the full synthetic batch
- `GET /cases?state=&leak_type=` — filterable case index for the timeline
- `GET /cases/{case_id}` — case, diagnosis, action ladder, attribution, and audit timeline
- `POST /actions/{action_id}/voice/turns` — idempotent simulated voice turn and bounded reply
- `GET /cases/{case_id}/audit.json` — exportable append-only audit record
- `GET /cases/{case_id}/replay` — stored projection, ordered events, replayed state
- `GET /suppressions` — currently open circuit breakers
- `POST /suppressions/{id}/close` — human circuit-breaker override
- `GET /costs` — LLM token, cost, latency, and schema-success rollup
- `GET /scoreboard/{run_id}` — holdout lift, incremental recovery, cost, and safety metrics
- `GET /scoreboard/latest` — most recent measured batch for the dashboard
- `GET /scoreboard/{run_id}/exceptions` — grouped reasons plus every non-recovered case
- `GET /evals/latest` — latest cohort and injection metrics with pass/fail gates
- `GET /health/live` and `GET /health/ready` — process and database health

Raw money values are paise (`BIGINT`) and timestamps are timezone-aware. Actuator calls and the
structured cohort transport are deterministic simulations in the default mode.

## Current boundary

The approved free-first live integration direction is documented in
[`API_INTEGRATION_PLAN.md`](API_INTEGRATION_PLAN.md).

The simulator feeds normalized synthetic signals directly into the shared case/event spine. The
scheduled checkout, invoice, subscription, and 24-hour reconciliation entry points and their
cadences are registered with Celery Beat, but their live upstream provider adapters still return
empty scan results until later integration slices. Diagnosis, planning, scheduling, pre-flight
gating, simulated interventions, payment-success cancellation, aggregate incident reasoning,
scope-specific suppression, and LLM accounting are built. Live provider calls and full recovery
verification outside the Razorpay success-webhook path remain reserved for later build slices.
Attribution, holdout enforcement, the scoreboard API, the reproducible September 1 evaluation
harness, the September 2 Scoreboard and Case Timeline, the September 3 bounded simulated voice,
and the September 4 full-batch outcome and exception workflow are built. Live voice transport is
not.
