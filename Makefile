.PHONY: install lint test test-coverage dashboard-check container-build release-infra fresh-migration-check batch-replay public-bundle-check ai-incident-evidence eval-gates security-tests acceptance-tests release-evidence demo-recording-check release-gate-automated release-gate test-august-27 test-august-28 test-august-29 test-api-august-29 test-api-august-30 test-api-august-31 test-api-september-1 test-api-september-2 test-api-september-3 test-api-september-4 test-august-30 test-august-31 test-september-1 test-september-2 test-september-3 test-september-4 evals sensitivity dashboard batch build up down migrate demo-webhook verify-foundation seed tunnel

PUBLIC_BUNDLE_CANARY := leakproof-release-browser-canary-2026-09-04
ACCEPTANCE_ARTIFACT_DIR ?= artifacts/api-acceptance
DEMO_RECORDING ?= artifacts/demo/leakproof-90s-backup.mp4

.PHONY: release-gate-isolated

install:
	uv sync --extra dev
	npm --prefix dashboard ci

lint:
	uv run ruff check .

test:
	uv run pytest

test-coverage:
	uv run pytest --cov=leakproof --cov-report=term-missing --cov-fail-under=85

dashboard-check:
	npm --prefix dashboard run check
	LEAKPROOF_OPERATOR_API_TOKEN=$(PUBLIC_BUNDLE_CANARY) npm --prefix dashboard run build

container-build:
	docker compose build api dashboard

release-infra:
	docker compose up -d --wait postgres redis
	docker compose run --rm migrate
	docker compose up -d api worker beat

fresh-migration-check:
	uv run python scripts/verify_fresh_migrations.py

batch-replay:
	LEAKPROOF_MODE=simulation LEAKPROOF_DATABASE_URL=postgresql+psycopg://leakproof:leakproof@localhost:55432/leakproof uv run python scripts/run_batch.py --verify-replay > /tmp/leakproof-release-gate-batch.json

public-bundle-check:
	uv run python scripts/verify_public_bundle.py --forbid $(PUBLIC_BUNDLE_CANARY)

ai-incident-evidence:
	uv run python scripts/capture_ai_incident_acceptance.py

eval-gates:
	LEAKPROOF_MODE=simulation LEAKPROOF_DATABASE_URL=postgresql+psycopg://leakproof:leakproof@localhost:55432/leakproof uv run python scripts/run_evals.py --report /tmp/leakproof-release-gate-evals.json

security-tests:
	uv run pytest tests/test_api_security.py

acceptance-tests:
	uv run pytest tests/test_api_september_4.py tests/test_foundation_verifier.py tests/test_acceptance_artifacts.py

release-evidence:
	uv run python scripts/validate_acceptance_artifacts.py --directory $(ACCEPTANCE_ARTIFACT_DIR) --require-live --require-both-hero-paths

demo-recording-check:
	uv run python scripts/validate_demo_recording.py --path $(DEMO_RECORDING)

release-gate-automated:
	$(MAKE) container-build
	$(MAKE) release-infra
	$(MAKE) fresh-migration-check
	$(MAKE) lint
	$(MAKE) test-coverage
	$(MAKE) dashboard-check
	$(MAKE) public-bundle-check
	$(MAKE) verify-foundation
	$(MAKE) verify-foundation
	$(MAKE) batch-replay
	$(MAKE) eval-gates
	$(MAKE) ai-incident-evidence
	$(MAKE) security-tests
	$(MAKE) acceptance-tests

release-gate: release-gate-automated
	$(MAKE) release-evidence

release-gate-isolated:
	uv run python scripts/run_isolated_release.py

test-august-27:
	uv run pytest tests/test_diagnosis.py tests/test_guardrails.py tests/test_templates.py

test-august-28:
	uv run pytest tests/test_policy.py

test-august-29:
	uv run pytest tests/test_actuators.py

test-api-august-29:
	uv run pytest tests/test_api_contracts.py tests/test_config.py tests/test_webhooks.py

test-api-august-30:
	uv run pytest tests/test_api_contracts.py tests/test_api_august_30.py

test-api-august-31:
	uv run pytest tests/test_api_contracts.py tests/test_api_august_30.py tests/test_api_august_31.py tests/test_webhooks.py

test-api-september-1:
	uv run pytest tests/test_api_contracts.py tests/test_api_august_30.py tests/test_api_august_31.py tests/test_api_september_1.py tests/test_webhooks.py

test-api-september-2:
	uv run pytest tests/test_api_contracts.py tests/test_api_august_30.py tests/test_api_august_31.py tests/test_api_september_1.py tests/test_api_september_2.py tests/test_webhooks.py

test-api-september-3:
	uv run pytest tests/test_api_contracts.py tests/test_api_august_30.py tests/test_api_august_31.py tests/test_api_september_1.py tests/test_api_september_2.py tests/test_api_september_3.py tests/test_webhooks.py
	npm --prefix dashboard run check
	npm --prefix dashboard run build

test-api-september-4:
	uv run pytest tests/test_api_contracts.py tests/test_api_august_30.py tests/test_api_august_31.py tests/test_api_september_1.py tests/test_api_september_2.py tests/test_api_september_3.py tests/test_api_september_4.py tests/test_webhooks.py
	npm --prefix dashboard run check
	npm --prefix dashboard run build

test-august-30:
	uv run pytest tests/test_tier2.py

test-august-31:
	uv run pytest tests/test_measurement.py

test-september-1:
	uv run pytest tests/test_evals.py

test-september-2:
	uv run pytest tests/test_dashboard_api.py
	npm --prefix dashboard run check
	npm --prefix dashboard run build

test-september-3:
	uv run pytest tests/test_voice.py

test-september-4:
	uv run pytest tests/test_batch.py

evals:
	LEAKPROOF_DATABASE_URL=postgresql+psycopg://leakproof:leakproof@localhost:55432/leakproof uv run python scripts/run_evals.py

sensitivity:
	uv run python scripts/run_sensitivity.py

dashboard:
	npm --prefix dashboard run dev

build:
	docker compose build api dashboard

up: build
	docker compose up -d postgres redis
	docker compose run --rm migrate
	docker compose up -d api worker beat dashboard

down:
	docker compose down

migrate: build
	docker compose run --rm migrate

demo-webhook:
	uv run python scripts/demo_webhook.py

tunnel:
	@command -v zrok >/dev/null 2>&1 || (echo "zrok is not installed or is not on PATH"; exit 1)
	@curl --fail --silent http://localhost:8000/health/ready >/dev/null || (echo "Local API is not ready on http://localhost:8000; run 'make up' first"; exit 1)
	zrok share public localhost:8000

verify-foundation:
	uv run python scripts/verify_foundation.py

seed:
	LEAKPROOF_DATABASE_URL=postgresql+psycopg://leakproof:leakproof@localhost:55432/leakproof uv run python scripts/seed_simulator.py

batch:
	LEAKPROOF_DATABASE_URL=postgresql+psycopg://leakproof:leakproof@localhost:55432/leakproof uv run python scripts/run_batch.py
