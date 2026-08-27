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

from leakproof.api.app import app  # noqa: E402
from leakproof.db import Base, get_session  # noqa: E402
from leakproof.models import db  # noqa: E402, F401


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
def client(session_factory, monkeypatch) -> Generator[TestClient, None, None]:
    def override_session() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr("leakproof.api.app.process_webhook.delay", lambda _: None)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
