from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from sqlalchemy import func, select

from leakproof.config import Settings
from leakproof.demo import DemoSessionCreateRequest
from leakproof.demo.contracts import DemoSessionState
from leakproof.demo.email import (
    execute_demo_recovery_email,
    schedule_demo_recovery_email,
)
from leakproof.demo.projection import get_demo_session_projection
from leakproof.demo.rate_limit import InMemoryRateLimiter
from leakproof.demo.service import create_demo_session
from leakproof.diagnosis import diagnose_case
from leakproof.models.db import DemoSession, EmailDelivery, EmailDeliveryEvent, WebhookEvent
from leakproof.models.domain import Arm, LeakType
from leakproof.providers import EmailSendRequest, ProviderError, ResendEmailProvider
from leakproof.providers.fakes import FakeEmailProvider, FakePaymentProvider
from leakproof.sensors.processor import process_stored_webhook
from leakproof.sensors.webhooks import (
    InvalidWebhookSignature,
    persist_webhook,
    verify_resend_signature,
)
from leakproof.services import NormalizedSignal, record_signal

NOW = datetime(2026, 9, 2, 6, 0, tzinfo=UTC)


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "mode": "simulation",
        "default_merchant_id": "merchant_resend_demo",
        "public_base_url": "https://demo.example.com",
        "recovery_token_secret": "september-2-resend-secret-long-enough",
        "razorpay_key_id": "rzp_test_september_2",
        "resend_api_key": "re_test",
        "resend_from_email": "Leakproof <demo@example.com>",
        "demo_email_allowlist": "reviewer@example.com,second@example.com",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def create_case(
    session,
    config: Settings,
    recipient: str | None = "reviewer@example.com",
    payment_provider: FakePaymentProvider | None = None,
):
    created = create_demo_session(
        session,
        DemoSessionCreateRequest(recipient=recipient),
        client_ip=f"203.0.113.{session.scalar(select(func.count(DemoSession.id))) or 1}",
        provider=payment_provider or FakePaymentProvider(),
        limiter=InMemoryRateLimiter(),
        settings=config,
        now=NOW,
    )
    demo = session.get(DemoSession, created.session_id)
    case, _ = record_signal(
        session,
        NormalizedSignal(
            merchant_id=demo.merchant_id,
            customer_id=demo.customer_id,
            leak_type=LeakType.PAYMENT_FAILURE,
            entity_type="payment",
            entity_id=f"pay_{created.session_id}",
            entity_root_id=demo.razorpay_order_id,
            amount_at_risk=demo.amount_paise,
            currency=demo.currency,
            evidence={
                "source": "razorpay_webhook",
                "session_id": demo.id,
                "method": "card",
                "error_reason": "gateway_technical_error",
            },
            occurred_at=NOW,
            dedupe_key_override=f"live:{demo.id}:{demo.razorpay_order_id}",
            arm_override=Arm.TREATMENT,
        ),
    )
    diagnose_case(session, case.id)
    demo.state = DemoSessionState.AT_RISK.value
    session.commit()
    action = schedule_demo_recovery_email(session, case.id, settings=config, now=NOW)
    return created, case, action


def test_resend_adapter_uses_registered_payload_and_action_idempotency_key():
    captured: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(request)
        return httpx2.Response(200, headers={"x-request-id": "req_1"}, json={"id": "email_1"})

    provider = ResendEmailProvider(
        "re_test",
        "demo@example.com",
        client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    result = provider.send_recovery_email(
        EmailSendRequest(
            action_id="act_1",
            case_id="case_1",
            recipient="reviewer@example.com",
            template_id="util_recovery_email_v1",
            template_variables={"subject": "Recover", "body": "Use the signed link"},
            idempotency_key="lp_action_1",
        )
    )

    assert result.provider_email_id == "email_1"
    assert captured[0].headers["idempotency-key"] == "lp_action_1"
    assert json.loads(captured[0].content) == {
        "from": "demo@example.com",
        "to": ["reviewer@example.com"],
        "subject": "Recover",
        "text": "Use the signed link",
    }


def test_resend_adapter_retries_transient_failure_and_rejects_malformed_success():
    attempts = 0

    def retry_handler(_request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        return (
            httpx2.Response(503, json={"name": "application_error"})
            if attempts == 1
            else httpx2.Response(200, json={"id": "email_after_retry"})
        )

    request = EmailSendRequest(
        action_id="act_1",
        case_id="case_1",
        recipient="reviewer@example.com",
        template_id="util_recovery_email_v1",
        template_variables={"subject": "Recover", "body": "Use the signed link"},
        idempotency_key="lp_action_1",
    )
    provider = ResendEmailProvider(
        "re_test",
        "demo@example.com",
        client=httpx2.Client(transport=httpx2.MockTransport(retry_handler)),
        sleep=lambda _: None,
    )
    assert provider.send_recovery_email(request).attempts == 2

    malformed = ResendEmailProvider(
        "re_test",
        "demo@example.com",
        client=httpx2.Client(
            transport=httpx2.MockTransport(lambda _: httpx2.Response(200, json={}))
        ),
    )
    with pytest.raises(ProviderError, match="malformed JSON") as error:
        malformed.send_recovery_email(request)
    assert error.value.retryable is False


def test_resend_timeout_retry_reuses_one_provider_idempotency_key():
    attempts: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts.append(request.headers["idempotency-key"])
        if len(attempts) == 1:
            raise httpx2.ReadTimeout("response lost after send", request=request)
        return httpx2.Response(200, json={"id": "email_same_provider_resource"})

    provider = ResendEmailProvider(
        "re_test",
        "demo@example.com",
        client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        sleep=lambda _: None,
    )
    result = provider.send_recovery_email(
        EmailSendRequest(
            action_id="act_timeout",
            case_id="case_timeout",
            recipient="reviewer@example.com",
            template_id="util_recovery_email_v1",
            template_variables={"subject": "Recover", "body": "Use the signed link"},
            idempotency_key="lp_action_timeout",
        )
    )

    assert attempts == ["lp_action_timeout", "lp_action_timeout"]
    assert result.provider_email_id == "email_same_provider_resource"
    assert result.attempts == 2


def test_allowlisted_delivery_is_delayed_and_idempotent(session_factory):
    config = settings()
    provider = FakeEmailProvider()
    with session_factory() as session:
        _, case, action = create_case(session, config)
        assert action.scheduled_for == NOW + timedelta(seconds=7)
        assert execute_demo_recovery_email(
            session, action.id, provider=provider, settings=config, now=NOW
        ).status == "not_due"

        sent = execute_demo_recovery_email(
            session,
            action.id,
            provider=provider,
            settings=config,
            now=NOW + timedelta(seconds=31),
        )
        replay = execute_demo_recovery_email(
            session,
            action.id,
            provider=provider,
            settings=config,
            now=NOW + timedelta(seconds=40),
        )

        assert sent.status == "sent"
        assert replay.replayed is True
        assert len(provider.calls) == 1
        assert provider.calls[0].idempotency_key == action.idempotency_key
        assert provider.calls[0].recipient == "reviewer@example.com"
        assert "https://demo.example.com/recover/" in provider.calls[0].template_variables["body"]
        assert session.scalar(
            select(func.count(EmailDelivery.id)).where(EmailDelivery.case_id == case.id)
        ) == 1


@pytest.mark.parametrize("recipient", [None, "not-allowlisted@example.com"])
def test_non_allowlisted_recipient_gets_preview_without_provider_call(
    session_factory, recipient
):
    config = settings()
    provider = FakeEmailProvider()
    with session_factory() as session:
        _, _, action = create_case(session, config, recipient)
        result = execute_demo_recovery_email(
            session,
            action.id,
            provider=provider,
            settings=config,
            now=NOW + timedelta(seconds=31),
        )

        assert result.status == "preview_only"
        assert provider.calls == []


def test_local_quota_blocks_before_provider_call(session_factory):
    config = settings(resend_daily_limit=1, resend_monthly_limit=2)
    provider = FakeEmailProvider()
    payment_provider = FakePaymentProvider()
    with session_factory() as session:
        _, _, first = create_case(
            session, config, "reviewer@example.com", payment_provider
        )
        assert execute_demo_recovery_email(
            session,
            first.id,
            provider=provider,
            settings=config,
            now=NOW + timedelta(seconds=31),
        ).status == "sent"
        _, _, second = create_case(
            session, config, "second@example.com", payment_provider
        )
        result = execute_demo_recovery_email(
            session,
            second.id,
            provider=provider,
            settings=config,
            now=NOW + timedelta(seconds=31),
        )

        assert result.status == "quota_blocked"
        assert len(provider.calls) == 1


def test_rolling_recipient_limit_allows_five_and_previews_the_sixth(session_factory):
    config = settings()
    email_provider = FakeEmailProvider()
    payment_provider = FakePaymentProvider()
    with session_factory() as session:
        statuses = []
        for _ in range(6):
            _, _, action = create_case(
                session,
                config,
                "reviewer@example.com",
                payment_provider,
            )
            statuses.append(
                execute_demo_recovery_email(
                    session,
                    action.id,
                    provider=email_provider,
                    settings=config,
                    now=NOW + timedelta(seconds=31),
                ).status
            )

        assert statuses == ["sent"] * 5 + ["rate_limited"]
        assert len(email_provider.calls) == 5


def _signed_headers(body: bytes, secret_bytes: bytes, timestamp: int) -> dict[str, str]:
    message_id = "msg_resend_1"
    signed = f"{message_id}.{timestamp}.".encode() + body
    signature = base64.b64encode(hmac.new(secret_bytes, signed, hashlib.sha256).digest()).decode()
    return {
        "message_id": message_id,
        "timestamp": str(timestamp),
        "signature": f"v1,{signature}",
        "secret": "whsec_" + base64.b64encode(secret_bytes).decode(),
    }


def test_resend_signature_verifies_raw_body_and_rejects_modification():
    body = b'{"type":"email.sent"}'
    headers = _signed_headers(body, b"resend-signing-key", int(NOW.timestamp()))
    verify_resend_signature(body, **headers, now=NOW)

    with pytest.raises(InvalidWebhookSignature):
        verify_resend_signature(body + b" ", **headers, now=NOW)


def test_resend_webhook_route_verifies_deduplicates_and_redacts(
    client, session_factory, monkeypatch
):
    secret_bytes = b"route-signing-key"
    webhook_secret = "whsec_" + base64.b64encode(secret_bytes).decode()
    config = settings(resend_webhook_secret=webhook_secret)
    monkeypatch.setattr("leakproof.api.app.get_settings", lambda: config)
    occurred_at = datetime.now(UTC)
    body = json.dumps(
        {
            "type": "email.delivered",
            "created_at": occurred_at.isoformat(),
            "data": {
                "email_id": "email_route_1",
                "to": ["must-not-be-persisted@example.com"],
            },
        },
        separators=(",", ":"),
    ).encode()
    signed = _signed_headers(body, secret_bytes, int(occurred_at.timestamp()))
    headers = {
        "svix-id": signed["message_id"],
        "svix-timestamp": signed["timestamp"],
        "svix-signature": signed["signature"],
        "content-type": "application/json",
    }

    first = client.post("/webhooks/resend", content=body, headers=headers)
    duplicate = client.post("/webhooks/resend", content=body, headers=headers)

    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert duplicate.json()["duplicate"] is True
    with session_factory() as session:
        webhook = session.scalar(
            select(WebhookEvent).where(WebhookEvent.provider == "resend")
        )
        assert webhook.payload["data"] == {"email_id": "email_route_1"}
        assert "must-not-be-persisted" not in json.dumps(webhook.payload)


def test_duplicate_and_out_of_order_delivery_events_converge(session_factory):
    config = settings()
    provider = FakeEmailProvider()
    with session_factory() as session:
        created, _, action = create_case(session, config)
        sent = execute_demo_recovery_email(
            session,
            action.id,
            provider=provider,
            settings=config,
            now=NOW + timedelta(seconds=31),
        )

        def ingest(kind: str, occurred_at: datetime, event_id: str):
            return persist_webhook(
                session,
                merchant_id=config.default_merchant_id,
                payload={
                    "type": kind,
                    "created_at": occurred_at.isoformat(),
                    "data": {"email_id": sent.provider_email_id},
                },
                header_event_id=event_id,
                provider="resend",
            )

        delivered = ingest("email.delivered", NOW + timedelta(minutes=2), "evt_delivered")
        process_stored_webhook(session, delivered.id)
        older_sent = ingest("email.sent", NOW + timedelta(minutes=1), "evt_sent")
        process_stored_webhook(session, older_sent.id)
        duplicate = ingest("email.sent", NOW + timedelta(minutes=1), "evt_sent")
        bounced = ingest("email.bounced", NOW + timedelta(minutes=3), "evt_bounced")
        process_stored_webhook(session, bounced.id)
        projection = get_demo_session_projection(
            session,
            created.session_id,
            session_token=created.session_token,
            settings=config,
            now=NOW + timedelta(minutes=3),
        )

        delivery = session.scalar(
            select(EmailDelivery).where(EmailDelivery.action_id == action.id)
        )
        assert duplicate.duplicate is True
        assert delivery.status == "bounced"
        assert any(
            item.provider == "resend"
            and item.operation == "delivery"
            and item.status == "bounced"
            for item in projection.provider_statuses
        )
        assert "reviewer@example.com" not in projection.model_dump_json()
        assert session.scalar(select(func.count(EmailDeliveryEvent.id))) == 3
        assert session.scalar(
            select(func.count(WebhookEvent.id)).where(WebhookEvent.provider == "resend")
        ) == 3


def test_delivery_webhook_processed_before_send_receipt_is_reconciled(session_factory):
    config = settings()
    provider = FakeEmailProvider()
    with session_factory() as session:
        _, _, action = create_case(session, config)
        early = persist_webhook(
            session,
            merchant_id=config.default_merchant_id,
            payload={
                "type": "email.delivered",
                "created_at": (NOW + timedelta(seconds=32)).isoformat(),
                "data": {"email_id": "email_fake_1"},
            },
            header_event_id="evt_early_delivery",
            provider="resend",
        )
        process_stored_webhook(session, early.id)

        result = execute_demo_recovery_email(
            session,
            action.id,
            provider=provider,
            settings=config,
            now=NOW + timedelta(seconds=31),
        )
        delivery = session.scalar(
            select(EmailDelivery).where(EmailDelivery.action_id == action.id)
        )

        assert result.status == "delivered"
        assert delivery.status == "delivered"


def test_expired_session_cancels_pending_email_without_provider_call(session_factory):
    from leakproof.audit.timeline import replay_case
    from leakproof.models.db import Event

    config = settings()
    provider = FakeEmailProvider()
    with session_factory() as session:
        created, case, action = create_case(session, config)
        expired = NOW + timedelta(minutes=config.demo_session_ttl_minutes + 1)
        for _ in range(2):
            result = execute_demo_recovery_email(
                session,
                action.id,
                provider=provider,
                settings=config,
                now=expired,
            )
            assert result.status == "cancelled"
        assert session.get(DemoSession, created.session_id).state == "EXPIRED"
        assert provider.calls == []
        assert (
            session.scalar(
                select(func.count())
                .select_from(Event)
                .where(
                    Event.case_id == case.id,
                    Event.kind == "ACTED",
                )
            )
            == 1
        )
        assert replay_case(session, case.id).projection_matches
