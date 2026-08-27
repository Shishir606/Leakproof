.PHONY: install lint test test-august-27 test-august-28 test-august-29 test-august-30 test-august-31 build up down migrate demo-webhook verify-foundation seed tunnel

install:
	uv sync --extra dev

lint:
	uv run ruff check .

test:
	uv run pytest

test-august-27:
	uv run pytest tests/test_diagnosis.py tests/test_guardrails.py tests/test_templates.py

test-august-28:
	uv run pytest tests/test_policy.py

test-august-29:
	uv run pytest tests/test_actuators.py

test-august-30:
	uv run pytest tests/test_tier2.py

test-august-31:
	uv run pytest tests/test_measurement.py

build:
	docker compose build api

up: build
	docker compose up -d postgres redis
	docker compose run --rm migrate
	docker compose up -d api worker beat

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
