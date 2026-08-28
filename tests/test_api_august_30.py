from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from sqlalchemy import func, select

from leakproof.config import Settings
from leakproof.demo import (
    CheckoutEventRequest,
    DemoSessionCreateRequest,
    DemoSessionState,
    live_case_dedupe_key,
)
from leakproof.demo.rate_limit import InMemoryRateLimiter
from leakproof.demo.security import decrypt_recipient
from leakproof.demo.service import (
    DemoRateLimitExceeded,
    DemoSessionUnauthorized,
    create_demo_session,
    due_abandonment_checks,
    ingest_checkout_event,
    materialize_checkout_abandonment,
)
from leakproof.models.db import (
    CheckoutEvent,
    Customer,
    DemoSession,
    Event,
    ProviderCall,
    RecoveryCase,
)
from leakproof.providers import (
    CreateOrderRequest,
    Payment,
    ProviderError,
    RazorpayPaymentProvider,
)
from leakproof.providers.fakes import FakePaymentProvider

NOW = datetime(2026, 8, 30, 6, 0, tzinfo=UTC)


def demo_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "mode": "simulation",
        "default_merchant_id": "merchant_live_demo",
        "recovery_token_secret": "test-session-secret-that-is-long-enough",
        "razorpay_key_id": "rzp_test_public",
        "demo_amount_paise": 50_000,
        "demo_session_ttl_minutes": 30,
        "demo_abandonment_delay_seconds": 30,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def create_session(
    session,
    *,
    provider: FakePaymentProvider | None = None,
    limiter: InMemoryRateLimiter | None = None,
    settings: Settings | None = None,
    recipient: str | None = None,
):
    provider = provider or FakePaymentProvider()
    limiter = limiter or InMemoryRateLimiter()
    settings = settings or demo_settings()
    created = create_demo_session(
        session,
        DemoSessionCreateRequest(recipient=recipient),
        client_ip="203.0.113.10",
        provider=provider,
        limiter=limiter,
        settings=settings,
        now=NOW,
    )
    return created, provider, limiter, settings


def checkout_event(event_type: str, client_event_id: str) -> CheckoutEventRequest:
    return CheckoutEventRequest.model_validate(
        {
            "client_event_id": client_event_id,
            "event_type": event_type,
            "occurred_at": NOW.isoformat(),
            "metadata": {},
        }
    )


def test_razorpay_order_adapter_uses_basic_auth_and_fixed_server_payload():
    captured: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["authorization"] = request.headers["authorization"]
        captured["json"] = json.loads(request.content)
        return httpx2.Response(
            200,
            headers={"x-razorpay-request-id": "req_rzp_1"},
            json={
                "id": "order_rzp_1",
                "amount": 50_000,
                "currency": "INR",
                "status": "created",
            },
        )

    client = httpx2.Client(
        base_url="https://api.razorpay.com",
        auth=httpx2.BasicAuth("rzp_test_key", "secret"),
        transport=httpx2.MockTransport(handler),
    )
    provider = RazorpayPaymentProvider("rzp_test_key", "secret", client=client)
    order = provider.create_order(
        CreateOrderRequest(
            amount_paise=50_000,
            currency="INR",
            receipt="demo-receipt",
            idempotency_key="demo-idempotency",
            notes={"session_id": "demo-1"},
        )
    )

    expected_auth = "Basic " + base64.b64encode(b"rzp_test_key:secret").decode()
    assert captured == {
        "method": "POST",
        "path": "/v1/orders",
        "authorization": expected_auth,
        "json": {
            "amount": 50_000,
            "currency": "INR",
            "receipt": "demo-receipt",
            "notes": {"session_id": "demo-1"},
        },
    }
    assert order.id == "order_rzp_1"
    assert order.request_id == "req_rzp_1"


def test_razorpay_adapter_retries_temporary_failure_and_rejects_mismatch():
    calls = 0

    def handler(_request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx2.Response(503)
        return httpx2.Response(
            200,
            json={"id": "order_wrong", "amount": 1, "currency": "INR", "status": "created"},
        )

    client = httpx2.Client(
        base_url="https://api.razorpay.com", transport=httpx2.MockTransport(handler)
    )
    provider = RazorpayPaymentProvider(
        "rzp_test_key", "secret", client=client, sleep=lambda _: None
    )
    with pytest.raises(ProviderError, match="amount or currency") as raised:
        provider.create_order(
            CreateOrderRequest(50_000, "INR", "receipt", "idempotency", {})
        )

    assert calls == 2
    assert raised.value.error_class == "response_mismatch"


def test_session_creation_persists_public_order_without_storing_plain_recipient(session_factory):
    settings = demo_settings(demo_email_allowlist="reviewer@example.com")
    with session_factory() as session:
        created, provider, _, _ = create_session(
            session,
            settings=settings,
            recipient="Reviewer@Example.com",
        )
        stored = session.get(DemoSession, created.session_id)
        customer = session.get(Customer, stored.customer_id)

        assert created.amount_paise == settings.demo_amount_paise
        assert created.email_mode.value == "allowlisted"
        assert created.razorpay_order_id == "order_fake_1"
        assert customer.id.startswith("demo_customer_")
        assert "reviewer@example.com" not in stored.recipient_ciphertext
        assert (
            decrypt_recipient(stored.recipient_ciphertext, settings.recovery_token_secret)
            == "reviewer@example.com"
        )
        assert provider.create_calls[0].amount_paise == settings.demo_amount_paise
        assert session.scalar(select(func.count()).select_from(ProviderCall)) == 1


def test_session_creation_uses_preview_for_non_allowlisted_recipient(session_factory):
    with session_factory() as session:
        created, _, _, _ = create_session(
            session,
            settings=demo_settings(demo_email_allowlist="reviewer@example.com"),
            recipient="public@example.com",
        )
        stored = session.get(DemoSession, created.session_id)

        assert created.email_mode.value == "preview_only"
        assert stored.recipient_ciphertext is None
        assert stored.recipient_hash is not None


def test_session_ip_limit_is_enforced_before_second_provider_call(session_factory):
    settings = demo_settings(demo_sessions_per_ip_hour=1)
    provider = FakePaymentProvider()
    limiter = InMemoryRateLimiter()
    with session_factory() as session:
        create_session(session, provider=provider, limiter=limiter, settings=settings)
        with pytest.raises(DemoRateLimitExceeded) as raised:
            create_session(session, provider=provider, limiter=limiter, settings=settings)

        assert raised.value.scope == "sessions_per_ip"
        assert len(provider.create_calls) == 1


def test_checkout_telemetry_is_token_bound_and_idempotent(session_factory):
    with session_factory() as session:
        created, _, limiter, settings = create_session(session)
        request = checkout_event("checkout_opened", "browser-event-1")
        first = ingest_checkout_event(
            session,
            created.session_id,
            request,
            session_token=created.session_token,
            limiter=limiter,
            settings=settings,
            now=NOW + timedelta(seconds=1),
        )
        duplicate = ingest_checkout_event(
            session,
            created.session_id,
            request,
            session_token=created.session_token,
            limiter=limiter,
            settings=settings,
            now=NOW + timedelta(seconds=2),
        )

        assert first.receipt.duplicate is False
        assert duplicate.receipt.duplicate is True
        assert duplicate.receipt.event_id == first.receipt.event_id
        assert session.scalar(select(func.count()).select_from(CheckoutEvent)) == 1
        assert session.get(DemoSession, created.session_id).state == DemoSessionState.CHECKOUT_OPEN

        with pytest.raises(DemoSessionUnauthorized):
            ingest_checkout_event(
                session,
                "another-session",
                checkout_event("checkout_opened", "browser-event-2"),
                session_token=created.session_token,
                limiter=limiter,
                settings=settings,
                now=NOW + timedelta(seconds=3),
            )


def test_dismissal_route_enqueues_once_and_duplicate_is_safe(client, monkeypatch):
    scheduled: list[dict] = []
    monkeypatch.setattr(
        "leakproof.api.app.check_demo_abandonment.apply_async",
        lambda **kwargs: scheduled.append(kwargs),
    )
    created = client.post("/demo/sessions", json={}).json()
    path = f"/demo/sessions/{created['session_id']}/checkout-events"
    headers = {"x-leakproof-session-token": created["session_token"]}
    payload = {
        "client_event_id": "dismiss-once",
        "event_type": "checkout_dismissed",
        "occurred_at": datetime.now(UTC).isoformat(),
        "metadata": {"dismissed_by": "customer"},
    }

    first = client.post(path, headers=headers, json=payload)
    duplicate = client.post(path, headers=headers, json=payload)

    assert first.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert scheduled == [
        {
            "args": [created["session_id"], first.json()["event_id"]],
            "countdown": 30,
        }
    ]


def test_due_dismissal_creates_one_live_abandonment_case(session_factory):
    with session_factory() as session:
        created, provider, limiter, settings = create_session(session)
        ingested = ingest_checkout_event(
            session,
            created.session_id,
            checkout_event("checkout_dismissed", "dismiss-1"),
            session_token=created.session_token,
            limiter=limiter,
            settings=settings,
            now=NOW + timedelta(seconds=1),
        )
        check_at = NOW + timedelta(seconds=31)

        case_id = materialize_checkout_abandonment(
            session,
            created.session_id,
            ingested.dismissal_event_id,
            provider=provider,
            settings=settings,
            now=check_at,
        )
        replayed = materialize_checkout_abandonment(
            session,
            created.session_id,
            ingested.dismissal_event_id,
            provider=provider,
            settings=settings,
            now=check_at + timedelta(seconds=1),
        )
        case = session.get(RecoveryCase, case_id)

        assert replayed == case_id
        assert case.leak_type == "CHECKOUT_ABANDON"
        assert case.dedupe_key == live_case_dedupe_key(
            created.session_id, created.razorpay_order_id
        )
        assert case.arm == "TREATMENT"
        assert session.get(DemoSession, created.session_id).state == DemoSessionState.AT_RISK
        assert session.scalar(select(func.count()).select_from(RecoveryCase)) == 1
        assert session.scalar(select(func.count()).select_from(Event)) == 2


@pytest.mark.parametrize("payment_status", ["failed", "authorized", "captured"])
def test_provider_payment_state_prevents_false_abandonment(session_factory, payment_status: str):
    provider = FakePaymentProvider()
    with session_factory() as session:
        created, _, limiter, settings = create_session(session, provider=provider)
        provider.payments["pay-existing"] = Payment(
            id="pay-existing",
            order_id=created.razorpay_order_id,
            amount_paise=created.amount_paise,
            currency="INR",
            status=payment_status,
        )
        ingested = ingest_checkout_event(
            session,
            created.session_id,
            checkout_event("checkout_dismissed", "dismiss-payment"),
            session_token=created.session_token,
            limiter=limiter,
            settings=settings,
            now=NOW + timedelta(seconds=1),
        )

        assert (
            materialize_checkout_abandonment(
                session,
                created.session_id,
                ingested.dismissal_event_id,
                provider=provider,
                settings=settings,
                now=NOW + timedelta(seconds=31),
            )
            is None
        )
        assert session.scalar(select(func.count()).select_from(RecoveryCase)) == 0


def test_later_attempt_invalidates_dismissal_and_rescue_scan(session_factory):
    with session_factory() as session:
        created, provider, limiter, settings = create_session(session)
        dismissed = ingest_checkout_event(
            session,
            created.session_id,
            checkout_event("checkout_dismissed", "dismiss-invalidated"),
            session_token=created.session_token,
            limiter=limiter,
            settings=settings,
            now=NOW + timedelta(seconds=1),
        )
        ingest_checkout_event(
            session,
            created.session_id,
            checkout_event("payment_attempt_started", "attempt-later"),
            session_token=created.session_token,
            limiter=limiter,
            settings=settings,
            now=NOW + timedelta(seconds=5),
        )

        assert due_abandonment_checks(
            session, settings=settings, now=NOW + timedelta(seconds=40)
        ) == []
        assert (
            materialize_checkout_abandonment(
                session,
                created.session_id,
                dismissed.dismissal_event_id,
                provider=provider,
                settings=settings,
                now=NOW + timedelta(seconds=40),
            )
            is None
        )
