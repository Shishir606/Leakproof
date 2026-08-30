from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect

from leakproof.config import Settings
from leakproof.demo import (
    CaseInsight,
    CheckoutEventRequest,
    CheckoutEventType,
    DemoSessionState,
    assert_session_transition,
    live_case_dedupe_key,
)
from leakproof.providers import (
    CaseInsightProvider,
    CreateOrderRequest,
    EmailProvider,
    PaymentProvider,
)
from leakproof.providers.fakes import (
    FakeCaseInsightProvider,
    FakeEmailProvider,
    FakePaymentProvider,
)


def live_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "mode": "live_demo",
        "public_base_url": "https://demo.example.com",
        "recovery_token_secret": "a" * 32,
        "razorpay_key_id": "rzp_test_contract",
        "razorpay_key_secret": "razorpay-secret",
        "razorpay_webhook_secret": "razorpay-webhook-secret",
        "openai_api_key": "openai-secret",
        "resend_api_key": "resend-secret",
        "resend_webhook_secret": "resend-webhook-secret",
        "resend_from_email": "demo@example.com",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_simulation_settings_need_no_provider_secrets():
    settings = Settings(_env_file=None, mode="simulation")

    assert settings.mode == "simulation"
    assert settings.openai_model == "gpt-5.6-luna"


def test_live_demo_settings_require_current_razorpay_boundary():
    with pytest.raises(ValidationError, match="configuration is incomplete"):
        Settings(_env_file=None, mode="live_demo")


def test_final_live_demo_requires_enabled_provider_configuration():
    with pytest.raises(ValidationError, match="openai_api_key"):
        live_settings(openai_api_key="")
    with pytest.raises(ValidationError, match="resend_api_key"):
        live_settings(resend_api_key="")


def test_provider_kill_switches_allow_safe_fallback_configuration():
    settings = live_settings(
        openai_api_key="",
        resend_api_key="",
        resend_from_email="",
        luna_enabled=False,
        outbound_email_enabled=False,
    )

    assert settings.luna_enabled is False
    assert settings.outbound_email_enabled is False


def test_live_demo_rejects_non_test_razorpay_keys():
    with pytest.raises(ValidationError, match="Razorpay test-mode key"):
        live_settings(razorpay_key_id="rzp_live_forbidden")


def test_live_demo_accepts_safe_configuration_and_normalizes_allowlist():
    settings = live_settings(
        demo_email_allowlist="Reviewer@Example.com, second@example.com"
    )

    assert settings.allowed_demo_emails == {
        "reviewer@example.com",
        "second@example.com",
    }


def test_live_case_key_is_shared_by_abandonment_and_failure_pipeline():
    assert live_case_dedupe_key("session_1", "order_1") == "live:session_1:order_1"


def test_session_transitions_are_explicit_and_terminal():
    assert_session_transition(DemoSessionState.CREATED, DemoSessionState.CHECKOUT_OPEN)
    assert_session_transition(DemoSessionState.AT_RISK, DemoSessionState.CHECKOUT_OPEN)
    assert_session_transition(DemoSessionState.RECOVERED, DemoSessionState.RECOVERED)

    with pytest.raises(ValueError, match="invalid demo-session transition"):
        assert_session_transition(DemoSessionState.RECOVERED, DemoSessionState.CHECKOUT_OPEN)


@pytest.mark.parametrize("event_type", list(CheckoutEventType))
def test_all_checkout_event_contracts_are_frozen(event_type: CheckoutEventType):
    request = CheckoutEventRequest.model_validate(
        {
            "client_event_id": f"client-{event_type.value}",
            "event_type": event_type.value,
            "occurred_at": "2026-08-29T09:00:00+05:30",
            "metadata": {"attempt_id": "attempt-1", "sdk_version": "1.0"},
        }
    )

    assert request.event_type == event_type
    assert request.occurred_at.tzinfo == UTC


def test_checkout_event_rejects_unknown_metadata():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        CheckoutEventRequest.model_validate(
            {
                "client_event_id": "client-1",
                "event_type": "checkout_opened",
                "occurred_at": datetime.now(UTC),
                "metadata": {"email": "must-not-be-accepted@example.com"},
            }
        )


def test_case_insight_rejects_unknown_or_unbounded_output():
    valid = {
        "summary": "The bank declined authorization.",
        "probable_cause": "Issuer decline",
        "evidence": ["Provider classification: declined"],
        "recommended_next_step": "Retry Checkout with customer authorization.",
        "confidence": 0.8,
    }
    assert CaseInsight.model_validate(valid).confidence == 0.8

    with pytest.raises(ValidationError):
        CaseInsight.model_validate({**valid, "confidence": 1.1, "unexpected": True})


def test_fake_adapters_implement_the_frozen_protocols():
    insight = CaseInsight(
        summary="Payment failed.",
        probable_cause="Issuer response",
        evidence=["declined"],
        recommended_next_step="Retry Checkout.",
        confidence=0.7,
    )
    payment = FakePaymentProvider()
    email = FakeEmailProvider()
    model = FakeCaseInsightProvider(result=insight)

    assert isinstance(payment, PaymentProvider)
    assert isinstance(email, EmailProvider)
    assert isinstance(model, CaseInsightProvider)
    order = payment.create_order(
        CreateOrderRequest(50_000, "INR", "receipt-1", "idem-1", {"session": "s1"})
    )
    assert order.id == "order_fake_1"


def test_live_demo_tables_are_present_in_test_metadata(session_factory):
    table_names = set(inspect(session_factory.kw["bind"]).get_table_names())

    assert {
        "demo_sessions",
        "checkout_events",
        "provider_calls",
        "email_deliveries",
        "email_delivery_events",
        "case_insights",
    } <= table_names


def assert_typed_error(response, expected_code: str = "integration_not_ready") -> None:
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": expected_code,
            "message": response.json()["error"]["message"],
            "retryable": True,
        }
    }


def test_demo_session_route_returns_frozen_checkout_contract(client):
    response = client.post("/demo/sessions", json={"recipient": "reviewer@example.com"})

    assert response.status_code == 201
    assert response.json()["razorpay_key_id"] == "rzp_test_simulated"
    assert response.json()["razorpay_order_id"] == "order_fake_1"
    assert response.json()["amount_paise"] == 50_000
    assert response.json()["currency"] == "INR"
    assert response.json()["email_mode"] == "preview_only"


@pytest.mark.parametrize("event_type", [event.value for event in CheckoutEventType])
def test_checkout_event_route_locks_all_four_event_types(client, event_type: str):
    created = client.post("/demo/sessions", json={}).json()
    response = client.post(
        f"/demo/sessions/{created['session_id']}/checkout-events",
        headers={"x-leakproof-session-token": created["session_token"]},
        json={
            "client_event_id": f"client-{event_type}",
            "event_type": event_type,
            "occurred_at": "2026-08-29T09:00:00Z",
            "metadata": {},
        },
    )

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "duplicate": False, "event_id": 1}


def test_checkout_event_route_requires_session_token_with_typed_error(client):
    response = client.post(
        "/demo/sessions/session-1/checkout-events",
        json={
            "client_event_id": "client-1",
            "event_type": "checkout_opened",
            "occurred_at": "2026-08-29T09:00:00Z",
        },
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "session_token_required"


def test_recovery_route_fails_closed_and_projection_remains_typed(client):
    recovery = client.get("/recover/signed-token")
    assert recovery.status_code == 404
    assert recovery.json() == {
        "error": {
            "code": "invalid_recovery_token",
            "message": "recovery link is invalid",
            "retryable": False,
        }
    }
    projection = client.get(
        "/demo/sessions/session-1",
        headers={"x-leakproof-session-token": "token-1"},
    )
    assert projection.status_code == 401
    assert projection.json()["error"]["code"] == "invalid_session_token"


@pytest.mark.parametrize(
    "event_type",
    [
        "email.sent",
        "email.delivered",
        "email.bounced",
        "email.complained",
        "email.clicked",
        "email.failed",
    ],
)
def test_resend_webhook_envelope_is_locked(client, event_type: str):
    response = client.post(
        "/webhooks/resend",
        json={
            "type": event_type,
            "created_at": "2026-08-29T09:00:00Z",
            "data": {"email_id": "email-1"},
        },
    )

    assert_typed_error(response)


def test_public_route_contracts_are_published_in_openapi(client):
    schema = client.get("/openapi.json").json()

    assert {
        "/demo/sessions",
        "/demo/sessions/{session_id}/checkout-events",
        "/demo/sessions/{session_id}",
        "/recover/{signed_token}",
        "/webhooks/razorpay",
        "/webhooks/resend",
    } <= set(schema["paths"])
