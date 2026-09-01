from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from leakproof.config import Settings
from leakproof.demo import CheckoutEventRequest, DemoSessionCreateRequest
from leakproof.demo.acceptance import build_demo_acceptance_export
from leakproof.demo.email import execute_demo_recovery_email, schedule_demo_recovery_email
from leakproof.demo.insights import generate_case_insight
from leakproof.demo.projection import get_demo_session_projection
from leakproof.demo.rate_limit import InMemoryRateLimiter
from leakproof.demo.service import create_demo_session, ingest_checkout_event
from leakproof.diagnosis import diagnose_case
from leakproof.models.db import DemoSession
from leakproof.providers.fakes import (
    FakeCaseInsightProvider,
    FakeEmailProvider,
    FakePaymentProvider,
)
from leakproof.sensors.processor import process_stored_webhook
from leakproof.sensors.webhooks import persist_webhook

NOW = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)


def settings() -> Settings:
    return Settings(
        _env_file=None,
        mode="simulation",
        default_merchant_id="merchant_september_3",
        public_base_url="https://demo.example.com",
        recovery_token_secret="september-3-dashboard-secret-long-enough",
        razorpay_key_id="rzp_test_september_3",
        resend_api_key="re_test",
        resend_from_email="demo@example.com",
        demo_email_allowlist="reviewer@example.com",
    )


def failure_payload(order_id: str) -> dict:
    return {
        "event": "payment.failed",
        "created_at": int((NOW + timedelta(seconds=2)).timestamp()),
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_september_3",
                    "order_id": order_id,
                    "amount": 50_000,
                    "currency": "INR",
                    "status": "failed",
                    "method": "card",
                    "error_source": "bank",
                    "error_step": "payment_authorization",
                    "error_reason": "gateway_technical_error",
                }
            }
        },
    }


def captured_payload(order_id: str) -> dict:
    return {
        "event": "payment.captured",
        "created_at": int((NOW + timedelta(minutes=2)).timestamp()),
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_september_3",
                    "order_id": order_id,
                    "amount": 50_000,
                    "currency": "INR",
                    "status": "captured",
                }
            }
        },
    }


def test_live_projection_exposes_dashboard_decisions_receipts_and_sanitized_sources(
    session_factory,
):
    config = settings()
    payment = FakePaymentProvider()
    email = FakeEmailProvider()
    with session_factory() as session:
        created = create_demo_session(
            session,
            DemoSessionCreateRequest(recipient="reviewer@example.com"),
            client_ip="203.0.113.93",
            provider=payment,
            limiter=InMemoryRateLimiter(),
            settings=config,
            now=NOW,
        )
        ingest_checkout_event(
            session,
            created.session_id,
            CheckoutEventRequest.model_validate(
                {
                    "client_event_id": "opened-september-3",
                    "event_type": "checkout_opened",
                    "occurred_at": (NOW + timedelta(seconds=1)).isoformat(),
                    "metadata": {"sdk_version": "v1"},
                }
            ),
            session_token=created.session_token,
            limiter=InMemoryRateLimiter(),
            settings=config,
            now=NOW + timedelta(seconds=1),
        )
        failed = persist_webhook(
            session,
            merchant_id=config.default_merchant_id,
            payload=failure_payload(created.razorpay_order_id),
            header_event_id="evt-september-3-failed",
        )
        case_id = process_stored_webhook(session, failed.id)
        diagnose_case(session, case_id)
        action = schedule_demo_recovery_email(session, case_id, settings=config, now=NOW)
        generate_case_insight(
            session,
            case_id,
            provider=FakeCaseInsightProvider(),
            settings=config,
        )
        sent = execute_demo_recovery_email(
            session,
            action.id,
            provider=email,
            settings=config,
            now=NOW + timedelta(seconds=31),
        )

        projection = get_demo_session_projection(
            session,
            created.session_id,
            session_token=created.session_token,
            settings=config,
            now=NOW + timedelta(seconds=45),
        )
        serialized = projection.model_dump_json()

        assert projection.case.leak_type == "PAYMENT_FAILURE"
        assert projection.case.deterministic_diagnosis["failure_class"] == "TRANSIENT"
        assert projection.gate_verdict == "ALLOW"
        assert [item.action_type for item in projection.recovery_actions] == [
            "recovery_link",
            "email_link",
        ]
        assert projection.recovery_actions[0].status == "available"
        assert projection.recovery_path is not None
        recovery_token = projection.recovery_path.removeprefix("/recover/")
        assert projection.recovery_actions[1].provider_receipt_id == sent.provider_email_id
        assert {item.source for item in projection.timeline} >= {
            "browser",
            "razorpay",
            "openai",
            "resend",
        }
        assert any(item.request_id for item in projection.provider_statuses)
        assert projection.end_to_end_latency_seconds == 43
        assert "reviewer@example.com" not in serialized
        assert "TREATMENT" not in serialized
        assert "holdout" not in serialized.casefold()
        assert "simulation" not in serialized.casefold()
        assert recovery_token not in json.dumps(
            [item.model_dump(mode="json") for item in projection.timeline]
        )
        acceptance = build_demo_acceptance_export(
            session,
            created.session_id,
            session_token=created.session_token,
            settings=config,
            now=NOW + timedelta(seconds=45),
        )
        assert recovery_token not in acceptance.model_dump_json()


def test_success_stops_live_polling_state_and_reports_verified_recovery(session_factory):
    config = settings()
    with session_factory() as session:
        created = create_demo_session(
            session,
            DemoSessionCreateRequest(),
            client_ip="203.0.113.94",
            provider=FakePaymentProvider(),
            limiter=InMemoryRateLimiter(),
            settings=config,
            now=NOW,
        )
        failed = persist_webhook(
            session,
            merchant_id=config.default_merchant_id,
            payload=failure_payload(created.razorpay_order_id),
            header_event_id="evt-september-3-failure-before-success",
        )
        case_id = process_stored_webhook(session, failed.id)
        paid = persist_webhook(
            session,
            merchant_id=config.default_merchant_id,
            payload=captured_payload(created.razorpay_order_id),
            header_event_id="evt-september-3-captured",
        )
        assert process_stored_webhook(session, paid.id) == case_id

        projection = get_demo_session_projection(
            session,
            created.session_id,
            session_token=created.session_token,
            settings=config,
            now=NOW + timedelta(minutes=3),
        )

        assert session.get(DemoSession, created.session_id).state == "RECOVERED"
        assert projection.state == "RECOVERED"
        assert projection.case.state == "CLOSED"
        assert projection.recovery_url_available is False
        assert projection.recovery_path is None
        assert projection.metrics.recovered_cases == 1
        assert projection.metrics.recovered_amount_paise == 50_000
        assert projection.metrics.recovery_rate == 1
        assert projection.end_to_end_latency_seconds == 118


def test_current_session_metrics_cannot_be_changed_by_global_recovery_history(
    session_factory,
):
    config = settings()
    provider = FakePaymentProvider()
    with session_factory() as session:
        recovered_session = create_demo_session(
            session,
            DemoSessionCreateRequest(),
            client_ip="203.0.113.95",
            provider=provider,
            limiter=InMemoryRateLimiter(),
            settings=config,
            now=NOW,
        )
        first_failure = persist_webhook(
            session,
            merchant_id=config.default_merchant_id,
            payload=failure_payload(recovered_session.razorpay_order_id),
            header_event_id="evt-history-failure",
        )
        process_stored_webhook(session, first_failure.id)
        first_paid = persist_webhook(
            session,
            merchant_id=config.default_merchant_id,
            payload=captured_payload(recovered_session.razorpay_order_id),
            header_event_id="evt-history-captured",
        )
        process_stored_webhook(session, first_paid.id)

        active_session = create_demo_session(
            session,
            DemoSessionCreateRequest(),
            client_ip="203.0.113.96",
            provider=provider,
            limiter=InMemoryRateLimiter(),
            settings=config,
            now=NOW + timedelta(minutes=4),
        )
        second_payload = failure_payload(active_session.razorpay_order_id)
        second_payload["payload"]["payment"]["entity"]["id"] = "pay_active_session"
        second_failure = persist_webhook(
            session,
            merchant_id=config.default_merchant_id,
            payload=second_payload,
            header_event_id="evt-active-failure",
        )
        process_stored_webhook(session, second_failure.id)
        projection = get_demo_session_projection(
            session,
            active_session.session_id,
            session_token=active_session.session_token,
            settings=config,
            now=NOW + timedelta(minutes=5),
        )

        assert projection.state == "AT_RISK"
        assert projection.metrics.cases_detected == 1
        assert projection.metrics.recovered_cases == 0
        assert projection.metrics.recovered_amount_paise == 0
        assert projection.environment_metrics.recovered_cases == 1
        assert projection.environment_metrics.recovered_amount_paise == 50_000
