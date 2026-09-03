from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from leakproof.config import Settings
from leakproof.demo import (
    CheckoutEventRequest,
    CheckoutPaymentVerificationRequest,
    DemoSessionCreateRequest,
)
from leakproof.demo.acceptance import build_demo_acceptance_export
from leakproof.demo.email import execute_demo_recovery_email, schedule_demo_recovery_email
from leakproof.demo.insights import generate_case_insight
from leakproof.demo.rate_limit import InMemoryRateLimiter
from leakproof.demo.security import issue_recovery_token
from leakproof.demo.service import (
    CheckoutPaymentNotCaptured,
    CheckoutPaymentProofInvalid,
    RecoveryExpired,
    create_demo_session,
    get_recovery_bootstrap,
    ingest_checkout_event,
    materialize_checkout_abandonment,
    verify_checkout_payment,
)
from leakproof.diagnosis import diagnose_case
from leakproof.models.db import DemoSession, ProviderCall
from leakproof.providers import Payment, ProviderError
from leakproof.providers.fakes import (
    FakeCaseInsightProvider,
    FakeEmailProvider,
    FakePaymentProvider,
)
from leakproof.sensors.processor import process_stored_webhook
from leakproof.sensors.webhooks import persist_webhook

NOW = datetime(2026, 9, 4, 6, 0, tzinfo=UTC)


def settings() -> Settings:
    return Settings(
        _env_file=None,
        mode="simulation",
        default_merchant_id="merchant_september_4",
        public_base_url="https://demo.example.com",
        recovery_token_secret="september-4-acceptance-secret-long-enough",
        razorpay_key_id="rzp_test_september_4",
        razorpay_key_secret="september-4-razorpay-key-secret",
        resend_api_key="re_test",
        resend_webhook_secret="whsec_test",
        resend_from_email="demo@example.com",
        demo_email_allowlist="reviewer@example.com",
        demo_session_ttl_minutes=60,
    )


def checkout_signature(order_id: str, payment_id: str, secret: str) -> str:
    return hmac.new(
        secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
    ).hexdigest()


def razorpay_payload(event_type: str, order_id: str, occurred_at: datetime) -> dict:
    entity = {
        "id": f"pay_{event_type.replace('.', '_')}",
        "order_id": order_id,
        "amount": 50_000,
        "currency": "INR",
        "status": "failed" if event_type == "payment.failed" else "captured",
    }
    if event_type == "payment.failed":
        entity.update(
            {
                "method": "card",
                "error_source": "bank",
                "error_step": "payment_authorization",
                "error_reason": "gateway_technical_error",
            }
        )
    return {
        "event": event_type,
        "created_at": int(occurred_at.timestamp()),
        "payload": {"payment": {"entity": entity}},
    }


def process_payload(session, payload: dict, event_id: str) -> tuple[str | None, bool]:
    stored = persist_webhook(
        session,
        merchant_id="merchant_september_4",
        payload=payload,
        header_event_id=event_id,
    )
    return process_stored_webhook(session, stored.id), stored.duplicate


@pytest.mark.parametrize(
    ("hero_path", "recipient", "luna_fallback"),
    [
        ("payment_failure", "reviewer@example.com", True),
        ("checkout_dismissal", None, False),
    ],
)
def test_both_hero_paths_finish_from_fresh_sessions_and_export_sanitized_evidence(
    session_factory,
    hero_path: str,
    recipient: str | None,
    luna_fallback: bool,
    request,
):
    config = settings()
    payment = FakePaymentProvider()
    email = FakeEmailProvider()
    with session_factory() as session:
        created = create_demo_session(
            session,
            DemoSessionCreateRequest(
                recipient=recipient,
                scenario_type="CHECKOUT_ABANDON"
                if hero_path == "checkout_dismissal"
                else "PAYMENT_FAILURE",
            ),
            client_ip="203.0.113.104",
            provider=payment,
            limiter=InMemoryRateLimiter(),
            settings=config,
            now=NOW,
        )

        if hero_path == "payment_failure":
            failed_payload = razorpay_payload(
                "payment.failed", created.razorpay_order_id, NOW + timedelta(seconds=2)
            )
            case_id, duplicate = process_payload(session, failed_payload, "evt-september-4-failure")
            duplicate_case, duplicate = process_payload(
                session, failed_payload, "evt-september-4-failure"
            )
            assert duplicate is True
            assert duplicate_case is None
        else:
            dismissed = ingest_checkout_event(
                session,
                created.session_id,
                CheckoutEventRequest.model_validate(
                    {
                        "client_event_id": "dismiss-september-4",
                        "event_type": "checkout_dismissed",
                        "occurred_at": (NOW + timedelta(seconds=1)).isoformat(),
                        "metadata": {
                            "attempt_id": "private-browser-attempt-id",
                            "dismissed_by": "customer",
                        },
                    }
                ),
                session_token=created.session_token,
                limiter=InMemoryRateLimiter(),
                settings=config,
                now=NOW + timedelta(seconds=1),
            )
            case_id = materialize_checkout_abandonment(
                session,
                created.session_id,
                dismissed.dismissal_event_id,
                provider=payment,
                settings=config,
                now=NOW + timedelta(seconds=31),
            )
        assert case_id is not None

        diagnose_case(session, case_id)
        action = schedule_demo_recovery_email(session, case_id, settings=config, now=NOW)
        insight_provider = FakeCaseInsightProvider(
            failure=(
                ProviderError(
                    provider="openai",
                    operation="case_insight",
                    error_class="timeout",
                    retryable=True,
                    message="simulated rehearsal timeout",
                )
                if luna_fallback
                else None
            )
        )
        insight = generate_case_insight(
            session,
            case_id,
            provider=insight_provider,
            settings=config,
        )
        assert insight.status == ("fallback" if luna_fallback else "succeeded")

        email_result = execute_demo_recovery_email(
            session,
            action.id,
            provider=email,
            settings=config,
            now=NOW + timedelta(seconds=35),
        )
        assert email_result.status == ("sent" if recipient else "preview_only")
        if recipient:
            delivered = persist_webhook(
                session,
                merchant_id=config.default_merchant_id,
                payload={
                    "type": "email.delivered",
                    "created_at": (NOW + timedelta(seconds=50)).isoformat(),
                    "data": {"email_id": email_result.provider_email_id},
                },
                header_event_id="evt-september-4-resend-delivered",
                provider="resend",
            )
            assert process_stored_webhook(session, delivered.id) == case_id

        recovery_token = issue_recovery_token(
            created.session_id,
            config.default_merchant_id,
            created.razorpay_order_id,
            created.amount_paise,
            created.currency,
            NOW - timedelta(seconds=1),
            config.recovery_token_secret,
        )
        with pytest.raises(RecoveryExpired):
            get_recovery_bootstrap(
                session,
                recovery_token,
                provider=payment,
                settings=config,
                now=NOW,
            )

        valid_token = issue_recovery_token(
            created.session_id,
            config.default_merchant_id,
            created.razorpay_order_id,
            created.amount_paise,
            created.currency,
            NOW + timedelta(minutes=10),
            config.recovery_token_secret,
            scenario_type=created.scenario_type,
        )
        reopened = get_recovery_bootstrap(
            session,
            valid_token,
            provider=payment,
            settings=config,
            now=NOW + timedelta(seconds=60),
        )
        assert reopened.razorpay_order_id == created.razorpay_order_id
        assert len(payment.create_calls) == 1

        closed_case_id, _ = process_payload(
            session,
            razorpay_payload(
                "payment.captured",
                created.razorpay_order_id,
                NOW + timedelta(minutes=2),
            ),
            f"evt-september-4-success-{hero_path}",
        )
        assert closed_case_id == case_id

        artifact = build_demo_acceptance_export(
            session,
            created.session_id,
            session_token=created.session_token,
            settings=config,
            now=NOW + timedelta(minutes=3),
        )
        serialized = artifact.model_dump_json()
        output = request.config.getoption("--acceptance-output-dir")
        if output:
            directory = Path(output)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"contract-{hero_path}.json").write_text(
                artifact.model_dump_json(indent=2) + "\n"
            )

        assert artifact.schema_version == "2026-09-04"
        assert artifact.passed is True, [
            check.check
            for check in artifact.checks
            if check.severity == "blocking" and not check.passed
        ]
        assert artifact.case is not None
        assert artifact.case.leak_type == (
            "PAYMENT_FAILURE" if hero_path == "payment_failure" else "CHECKOUT_ABANDON"
        )
        assert artifact.case.state == "CLOSED"
        assert artifact.operational_metrics.recovered_cases == 1
        assert all(check.passed for check in artifact.checks if check.severity == "blocking")
        checks = {check.check: check.passed for check in artifact.checks}
        assert checks["original_order_reused"] is True
        assert checks["provider_verified_payment"] is True
        assert checks["pending_contacts_cancelled"] is True
        assert checks["session_recovered_amount_correct"] is True
        assert checks["audit_projection_replay_matches"] is True
        assert checks["no_blocking_provider_failure"] is True
        assert "reviewer@example.com" not in serialized
        assert "private-browser-attempt-id" not in serialized
        assert created.session_id not in serialized
        assert created.razorpay_order_id not in serialized
        assert "request_id" not in serialized
        assert "session_token" not in serialized


def test_signed_checkout_and_captured_api_truth_close_without_a_success_webhook(
    session_factory,
):
    config = settings()
    payment = FakePaymentProvider()
    with session_factory() as session:
        created = create_demo_session(
            session,
            DemoSessionCreateRequest(),
            client_ip="203.0.113.106",
            provider=payment,
            limiter=InMemoryRateLimiter(),
            settings=config,
            now=NOW,
        )
        case_id, _ = process_payload(
            session,
            razorpay_payload(
                "payment.failed", created.razorpay_order_id, NOW + timedelta(seconds=1)
            ),
            "evt-test-mode-failure-before-signed-checkout",
        )
        diagnose_case(session, case_id)
        action = schedule_demo_recovery_email(session, case_id, settings=config, now=NOW)
        execute_demo_recovery_email(
            session,
            action.id,
            provider=FakeEmailProvider(),
            settings=config,
            now=NOW + timedelta(seconds=31),
        )
        generate_case_insight(
            session,
            case_id,
            provider=FakeCaseInsightProvider(),
            settings=config,
        )

        payment_id = "pay_signed_test_mode_1"
        payment.payments[payment_id] = Payment(
            id=payment_id,
            order_id=created.razorpay_order_id,
            amount_paise=created.amount_paise,
            currency=created.currency,
            status="captured",
            method="card",
            request_id="req_provider_payment_check",
        )
        proof = CheckoutPaymentVerificationRequest(
            razorpay_payment_id=payment_id,
            razorpay_order_id=created.razorpay_order_id,
            razorpay_signature=checkout_signature(
                created.razorpay_order_id, payment_id, config.razorpay_key_secret
            ),
        )

        result = verify_checkout_payment(
            session,
            created.session_id,
            proof,
            provider=payment,
            limiter=InMemoryRateLimiter(),
            settings=config,
            session_token=created.session_token,
            now=NOW + timedelta(minutes=2),
        )
        duplicate = verify_checkout_payment(
            session,
            created.session_id,
            proof,
            provider=payment,
            limiter=InMemoryRateLimiter(),
            settings=config,
            session_token=created.session_token,
            now=NOW + timedelta(minutes=2, seconds=1),
        )
        artifact = build_demo_acceptance_export(
            session,
            created.session_id,
            session_token=created.session_token,
            settings=config,
            now=NOW + timedelta(minutes=3),
        )
        serialized = artifact.model_dump_json()

        assert result.verified is True
        assert result.duplicate is False
        assert duplicate.duplicate is True
        assert session.get(DemoSession, created.session_id).state == "RECOVERED"
        assert artifact.passed is True
        assert {check.check: check.passed for check in artifact.checks}[
            "provider_verified_payment"
        ] is True
        assert payment_id not in serialized
        assert proof.razorpay_signature not in serialized
        verification = session.scalar(
            select(ProviderCall).where(
                ProviderCall.session_id == created.session_id,
                ProviderCall.operation == "verify_checkout_payment",
                ProviderCall.status == "succeeded",
            )
        )
        assert verification.safe_response_metadata == {
            "verification_source": "checkout_signature_plus_payment_api",
            "signature_checked_server_side": True,
            "payment_status": "captured",
        }


def test_checkout_proof_fails_closed_for_bad_signature_or_uncaptured_payment(
    session_factory,
):
    config = settings()
    payment = FakePaymentProvider()
    with session_factory() as session:
        created = create_demo_session(
            session,
            DemoSessionCreateRequest(),
            client_ip="203.0.113.107",
            provider=payment,
            limiter=InMemoryRateLimiter(),
            settings=config,
            now=NOW,
        )
        process_payload(
            session,
            razorpay_payload(
                "payment.failed", created.razorpay_order_id, NOW + timedelta(seconds=1)
            ),
            "evt-test-mode-failure-before-invalid-proof",
        )
        payment_id = "pay_authorized_test_mode_1"
        payment.payments[payment_id] = Payment(
            id=payment_id,
            order_id=created.razorpay_order_id,
            amount_paise=created.amount_paise,
            currency=created.currency,
            status="authorized",
        )
        valid_signature = checkout_signature(
            created.razorpay_order_id, payment_id, config.razorpay_key_secret
        )

        with pytest.raises(CheckoutPaymentProofInvalid):
            verify_checkout_payment(
                session,
                created.session_id,
                CheckoutPaymentVerificationRequest(
                    razorpay_payment_id=payment_id,
                    razorpay_order_id=created.razorpay_order_id,
                    razorpay_signature="0" * 64,
                ),
                provider=payment,
                limiter=InMemoryRateLimiter(),
                settings=config,
                session_token=created.session_token,
                now=NOW + timedelta(minutes=1),
            )
        with pytest.raises(CheckoutPaymentNotCaptured):
            verify_checkout_payment(
                session,
                created.session_id,
                CheckoutPaymentVerificationRequest(
                    razorpay_payment_id=payment_id,
                    razorpay_order_id=created.razorpay_order_id,
                    razorpay_signature=valid_signature,
                ),
                provider=payment,
                limiter=InMemoryRateLimiter(),
                settings=config,
                session_token=created.session_token,
                now=NOW + timedelta(minutes=1, seconds=1),
            )

        assert session.get(DemoSession, created.session_id).state == "AT_RISK"
        statuses = list(
            session.scalars(
                select(ProviderCall.status).where(
                    ProviderCall.session_id == created.session_id,
                    ProviderCall.operation == "verify_checkout_payment",
                )
            )
        )
        assert statuses == ["failed", "pending"]


def test_acceptance_export_route_is_token_protected_and_reports_open_checks(client):
    created = client.post("/demo/sessions", json={}).json()
    endpoint = f"/demo/sessions/{created['session_id']}/acceptance.json"

    assert client.get(endpoint).status_code == 401
    response = client.get(
        endpoint,
        headers={"x-leakproof-session-token": created["session_token"]},
    )

    assert response.status_code == 200
    assert response.json()["schema_version"] == "2026-09-04"
    assert response.json()["passed"] is False
    assert response.json()["case"] is None


def test_test_mode_checkout_verification_route_is_token_bound_and_idempotent(
    client, payment_provider, monkeypatch
):
    config = settings()
    monkeypatch.setattr("leakproof.api.app.get_settings", lambda: config)
    created = client.post("/demo/sessions", json={}).json()
    endpoint = f"/demo/sessions/{created['session_id']}/payments/verify"
    payment_id = "pay_route_test_mode_1"
    payment_provider.payments[payment_id] = Payment(
        id=payment_id,
        order_id=created["razorpay_order_id"],
        amount_paise=created["amount_paise"],
        currency=created["currency"],
        status="captured",
    )
    body = {
        "razorpay_payment_id": payment_id,
        "razorpay_order_id": created["razorpay_order_id"],
        "razorpay_signature": checkout_signature(
            created["razorpay_order_id"], payment_id, config.razorpay_key_secret
        ),
    }

    assert client.post(endpoint, json=body).json()["error"]["code"] == (
        "payment_verification_token_required"
    )
    headers = {"x-leakproof-session-token": created["session_token"]}
    first = client.post(endpoint, headers=headers, json=body)
    duplicate = client.post(endpoint, headers=headers, json=body)

    assert first.status_code == 200
    assert first.json() == {
        "verified": True,
        "duplicate": False,
        "state": "RECOVERED",
        "payment_status": "captured",
    }
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True


def test_new_session_kill_switch_returns_typed_retryable_error(client, monkeypatch):
    disabled = Settings(_env_file=None, mode="simulation", demo_sessions_enabled=False)
    monkeypatch.setattr("leakproof.api.app.get_settings", lambda: disabled)

    response = client.post("/demo/sessions", json={})

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "demo_sessions_disabled",
            "message": "new demo sessions are temporarily disabled",
            "retryable": True,
        }
    }


def test_release_kill_switches_keep_recovery_non_blocking(session_factory):
    config = settings().model_copy(update={"luna_enabled": False, "outbound_email_enabled": False})
    with session_factory() as session:
        created = create_demo_session(
            session,
            DemoSessionCreateRequest(recipient="reviewer@example.com"),
            client_ip="203.0.113.105",
            provider=FakePaymentProvider(),
            limiter=InMemoryRateLimiter(),
            settings=config,
            now=NOW,
        )
        case_id, _ = process_payload(
            session,
            razorpay_payload(
                "payment.failed", created.razorpay_order_id, NOW + timedelta(seconds=1)
            ),
            "evt-september-4-kill-switch",
        )
        diagnose_case(session, case_id)
        action = schedule_demo_recovery_email(session, case_id, settings=config, now=NOW)

        insight = generate_case_insight(
            session,
            case_id,
            provider=FakeCaseInsightProvider(),
            settings=config,
        )
        email = execute_demo_recovery_email(
            session,
            action.id,
            provider=FakeEmailProvider(),
            settings=config,
            now=NOW + timedelta(seconds=31),
        )

        assert insight.status == "fallback"
        assert insight.fallback_reason == "disabled"
        assert email.status == "preview_only"
