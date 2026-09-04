# Leakproof

Leakproof is an auditable revenue-recovery system built with FastAPI, PostgreSQL, Redis, Celery,
and Next.js. It receives and verifies payment-provider events, stores them in a durable inbox,
creates recovery cases, applies deterministic diagnosis and policy checks, executes bounded
actions, and records an append-only audit timeline.

The repository supports two operating modes:

- `simulation` (default): runs reproducible synthetic scenarios without external provider keys.
- `live_demo`: connects the demo to Razorpay Test Mode and, when enabled, OpenAI and Resend. The
  application rejects Razorpay Live Mode keys.

## Implemented capabilities

| Capability | Status |
|---|---|
| Razorpay test-mode payment-failure and checkout-abandonment recovery | `LIVE_PROVIDER_VERIFIED` |
| Simulated payment failure, checkout abandonment, overdue invoice, and halted subscription scenarios | `SIMULATED_END_TO_END` |
| Voice/provider adapter boundaries | `ARCHITECTURE_READY` |

The same contract is available from `GET /capabilities`. Live-provider evidence is test-mode
evidence; simulated results are not production revenue claims.

## How it works

```text
Provider webhook or simulator
            |
            v
        FastAPI API
            |
            v
 Durable PostgreSQL inbox ----> Celery worker
                                      |
                                      v
                         Case, policy, and actions
                                      |
                                      v
                         Append-only audit timeline
```

- FastAPI exposes health, capability, webhook, demo, case, scoreboard, and audit endpoints.
- PostgreSQL stores inbox events, cases, projections, and audit records.
- Redis is the Celery broker.
- Celery workers process events; Celery Beat runs scheduled reconciliation and inbox rescans.
- The Next.js dashboard displays the Live Demo and synthetic Scenario Lab.
- The simulator creates reproducible data from `simulator/params.yaml`.

## Prerequisites

For the recommended Docker setup, install Docker Desktop with Docker Compose, `make`, and `curl`.

For development outside Docker, also install Python 3.12 or newer, `uv`, Node.js 22, and npm.

## Setup with Docker (recommended)

### 1. Create the environment file

From the repository root:

```bash
cp .env.example .env
```

The default is `LEAKPROOF_MODE=simulation`, so external provider keys are not required.

Generate an operator token and place its output in `.env` as
`LEAKPROOF_OPERATOR_API_TOKEN`:

```bash
openssl rand -base64 32
```

Keep `LEAKPROOF_OPERATOR_MERCHANT_IDS=merchant_demo` unless you intentionally use a different
merchant scope. The token protects operational case, scoreboard, cost, evaluation, suppression,
batch, voice, replay, and audit endpoints.

### 2. Build and start the stack

Make sure Docker Desktop is running, then execute:

```bash
make up
```

This builds the API and dashboard images, starts PostgreSQL and Redis, applies Alembic migrations,
and starts the API, Celery worker, Celery Beat, and dashboard.

### 3. Verify the API

```bash
curl http://localhost:8000/health/ready
curl http://localhost:8000/capabilities
```

### 4. Open the dashboard

Open [http://localhost:3000](http://localhost:3000). The synthetic dashboard is at
[http://localhost:3000/scenario-lab](http://localhost:3000/scenario-lab).

### 5. Seed the simulator

```bash
make seed
```

This uses seed `42`, persists the dataset to PostgreSQL, and writes
`artifacts/simulator/seed-42.json`. Repeating it reuses the same synthetic identities instead of
creating duplicates.

### 6. Run the recovery batch

```bash
make batch
```

This processes simulated cases through diagnosis, planning, guardrails, actions, outcome
measurement, and the exception report.

### 7. Stop the stack

```bash
make down
```

PostgreSQL data remains in its Docker volume. To intentionally remove it too, run:

```bash
docker compose down --volumes
```

## Local development

The application still needs PostgreSQL and Redis. Start those services:

```bash
docker compose up -d --wait postgres redis
```

Install Python and dashboard dependencies:

```bash
make install
```

Set host-accessible service URLs:

```bash
export LEAKPROOF_DATABASE_URL=postgresql+psycopg://leakproof:leakproof@localhost:55432/leakproof
export LEAKPROOF_REDIS_URL=redis://localhost:6379/0
```

Apply migrations and start the API:

```bash
uv run alembic upgrade head
uv run uvicorn leakproof.api.app:app --reload --port 8000
```

In separate terminals, start the worker and scheduler:

```bash
uv run celery -A leakproof.celery_app:celery worker --loglevel=INFO
```

```bash
uv run celery -A leakproof.celery_app:celery beat --loglevel=INFO
```

Start the dashboard:

```bash
API_BASE_URL=http://localhost:8000 make dashboard
```

## Tests and checks

```bash
make test                 # Python tests
make lint                 # Ruff linting
make test-coverage        # Tests with the 85% coverage gate
make dashboard-check      # Type-check and build the dashboard
make verify-foundation    # End-to-end check against the running stack
```

Run the automated repository release checks with:

```bash
make release-gate-automated
```

`make release-gate` additionally requires captured live-provider rehearsal artifacts. Read
[`docs/API_RELEASE_RUNBOOK.md`](docs/API_RELEASE_RUNBOOK.md) before using it.

## Optional live demo

Live Demo mode is a sandbox/test-mode integration. Before changing `LEAKPROOF_MODE` to
`live_demo`, follow [`docs/API_RELEASE_RUNBOOK.md`](docs/API_RELEASE_RUNBOOK.md).

The application validates at least these settings:

- An HTTPS `LEAKPROOF_PUBLIC_BASE_URL`
- A random `LEAKPROOF_OPERATOR_API_TOKEN` of at least 32 bytes
- A `LEAKPROOF_RECOVERY_TOKEN_SECRET` of at least 32 characters
- Razorpay Test Mode key ID and secret
- A Resend webhook secret
- An OpenAI API key when Luna enrichment is enabled
- A Resend API key and sender address when outbound email is enabled

The kill switches in `.env.example` can disable new demo sessions, Luna calls, or outbound email.
Webhook registration, allowlisting, provider-specific settings, rehearsal steps, and rollback are
documented in the runbook.

## Project structure

```text
src/leakproof/       Python application, API, workers, policies, providers, and simulator logic
dashboard/           Next.js dashboard
config/              Diagnosis, policy, action, guardrail, and measurement configuration
migrations/          Alembic database migrations
simulator/           Fixed-seed simulation parameters
scripts/             Seed, verification, evaluation, and release scripts
tests/               Python test suite
evals/               Evaluation datasets and reports
docs/                Operational and release documentation
```

## License

See [`LICENSE`](LICENSE).
