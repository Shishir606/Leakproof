from __future__ import annotations

import os
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("LEAKPROOF_ENVIRONMENT", "test")
os.environ.setdefault("LEAKPROOF_RAZORPAY_WEBHOOK_SECRET", "test-secret")
os.environ["LEAKPROOF_OPERATOR_API_TOKEN"] = "test-operator-token-that-is-at-least-32-bytes"
os.environ["LEAKPROOF_OPERATOR_MERCHANT_IDS"] = "*"
# Keep the test process isolated from a developer's credential-bearing .env file.
os.environ["LEAKPROOF_MODE"] = "simulation"
os.environ["LEAKPROOF_RAZORPAY_KEY_ID"] = ""
os.environ["LEAKPROOF_RAZORPAY_KEY_SECRET"] = ""
os.environ["LEAKPROOF_OPENAI_API_KEY"] = ""
os.environ["LEAKPROOF_RESEND_API_KEY"] = ""
os.environ["LEAKPROOF_RESEND_WEBHOOK_SECRET"] = ""
os.environ["LEAKPROOF_RESEND_FROM_EMAIL"] = ""

from leakproof.api.app import app  # noqa: E402
from leakproof.db import Base, get_session  # noqa: E402
from leakproof.demo.rate_limit import InMemoryRateLimiter  # noqa: E402
from leakproof.models import db  # noqa: E402, F401
from leakproof.providers.factory import (  # noqa: E402
    get_demo_rate_limiter,
    get_payment_provider,
)
from leakproof.providers.fakes import FakePaymentProvider  # noqa: E402


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def payment_provider():
    return FakePaymentProvider()


@pytest.fixture
def demo_rate_limiter():
    return InMemoryRateLimiter()


@pytest.fixture
def client(
    session_factory, monkeypatch, payment_provider, demo_rate_limiter
) -> Generator[TestClient, None, None]:
    def override_session() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_payment_provider] = lambda: payment_provider
    app.dependency_overrides[get_demo_rate_limiter] = lambda: demo_rate_limiter
    monkeypatch.setattr("leakproof.api.app.process_webhook.delay", lambda _: None)
    monkeypatch.setattr("leakproof.api.app.check_demo_abandonment.apply_async", lambda **_: None)
    with TestClient(
        app,
        headers={
            "Authorization": "Bearer test-operator-token-that-is-at-least-32-bytes"
        },
    ) as test_client:
        yield test_client
    app.dependency_overrides.clear()
