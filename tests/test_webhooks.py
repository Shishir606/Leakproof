from __future__ import annotations

import hashlib
import hmac
import json

from sqlalchemy import func, select

from leakproof.models.db import (
    Event,
    PaymentAttemptObservation,
    RecoveryCase,
    WebhookEvent,
)
from leakproof.sensors.processor import process_stored_webhook


def payment_failed(payment_id: str) -> dict:
    return {
        "event": "payment.failed",
        "created_at": 1787625000,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": "order_same",
                    "customer_id": "customer_same",
                    "amount": 250_000,
                    "currency": "INR",
                    "method": "netbanking",
                    "bank": "HDFC",
                    "error_source": "bank",
                    "error_step": "payment_authorization",
                    "error_reason": "gateway_technical_error",
                }
            }
        },
    }


def payment_captured(payment_id: str, order_id: str) -> dict:
    return {
        "event": "payment.captured",
        "created_at": 1787625060,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": 250_000,
                    "currency": "INR",
                    "status": "captured",
                    "method": "card",
                    "card": {"issuer": "HDFC", "iin": "411111"},
                }
            }
        },
    }


def order_paid(order_id: str) -> dict:
    return {
        "event": "order.paid",
        "created_at": 1787625070,
        "payload": {
            "order": {
                "entity": {
                    "id": order_id,
                    "amount": 250_000,
                    "amount_paid": 250_000,
                    "currency": "INR",
                    "status": "paid",
                }
            }
        },
    }


def signed_body(payload: dict) -> tuple[bytes, str]:
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    return body, signature


def post_webhook(client, payload: dict, event_id: str):
    body, signature = signed_body(payload)
    return client.post(
        "/webhooks/razorpay",
        content=body,
        headers={
            "content-type": "application/json",
            "x-razorpay-signature": signature,
            "x-razorpay-event-id": event_id,
            "x-leakproof-merchant-id": "merchant_test",
        },
    )


def test_duplicate_provider_event_is_persisted_once(client, session_factory):
    payload = payment_failed("pay_1")

    first = post_webhook(client, payload, "rzp_evt_same")
    duplicate = post_webhook(client, payload, "rzp_evt_same")

    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert duplicate.status_code == 200
    assert duplicate.json() == {
        "accepted": True,
        "duplicate": True,
        "webhook_id": first.json()["webhook_id"],
    }
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WebhookEvent)) == 1


def test_duplicate_without_provider_event_header_uses_stable_fingerprint(client, session_factory):
    body, signature = signed_body(payment_failed("pay_fingerprint"))
    headers = {
        "content-type": "application/json",
        "x-razorpay-signature": signature,
    }

    first = client.post("/webhooks/razorpay", content=body, headers=headers)
    duplicate = client.post("/webhooks/razorpay", content=body, headers=headers)

    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert duplicate.json()["duplicate"] is True
    with session_factory() as session:
        stored = session.scalars(select(WebhookEvent)).one()
        assert stored.provider_event_key.startswith("fp_")


def test_three_failures_become_one_case_and_three_events(client, session_factory):
    webhook_ids: list[int] = []
    for index in range(3):
        response = post_webhook(
            client,
            payment_failed(f"pay_{index}"),
            f"rzp_evt_{index}",
        )
        webhook_ids.append(response.json()["webhook_id"])

    with session_factory() as session:
        for webhook_id in webhook_ids:
            process_stored_webhook(session, webhook_id)

        cases = list(session.scalars(select(RecoveryCase)))
        events = list(session.scalars(select(Event).order_by(Event.seq)))
        inbox = list(session.scalars(select(WebhookEvent)))

    assert len(cases) == 1
    assert cases[0].dedupe_key == "pf:customer_same:order_same"
    assert [event.kind for event in events] == [
        "DETECTED",
        "ASSIGNED",
        "SIGNAL",
        "SIGNAL",
    ]
    assert all(event.processed_at is not None for event in inbox)


def test_worker_redelivery_does_not_append_another_case_event(client, session_factory):
    response = post_webhook(client, payment_failed("pay_redelivery"), "rzp_evt_redelivery")
    webhook_id = response.json()["webhook_id"]

    with session_factory() as session:
        case_id = process_stored_webhook(session, webhook_id)
        redelivered_case_id = process_stored_webhook(session, webhook_id)

        assert case_id is not None
        assert redelivered_case_id is None
        assert session.scalar(select(func.count()).select_from(RecoveryCase)) == 1
        assert session.scalar(select(func.count()).select_from(Event)) == 2
        assert session.get(WebhookEvent, webhook_id).processing_attempts == 1


def test_duplicate_failure_payment_is_one_observed_attempt(client, session_factory):
    ids = []
    for event_id in ("rzp_failure_delivery_1", "rzp_failure_delivery_2"):
        response = post_webhook(client, payment_failed("pay_same_attempt"), event_id)
        ids.append(response.json()["webhook_id"])

    with session_factory() as session:
        for webhook_id in ids:
            process_stored_webhook(session, webhook_id)
        observations = list(session.scalars(select(PaymentAttemptObservation)))

    assert len(observations) == 1
    assert observations[0].outcome == "failure"
    assert observations[0].method == "netbanking"
    assert observations[0].issuer == "HDFC"


def test_captured_and_order_paid_reconcile_to_one_success(client, session_factory):
    captured = post_webhook(
        client, payment_captured("pay_captured", "order_captured"), "rzp_captured"
    )
    paid = post_webhook(client, order_paid("order_captured"), "rzp_order_paid")

    with session_factory() as session:
        process_stored_webhook(session, captured.json()["webhook_id"])
        process_stored_webhook(session, paid.json()["webhook_id"])
        observations = list(session.scalars(select(PaymentAttemptObservation)))

    assert len(observations) == 1
    assert observations[0].outcome == "success"
    assert observations[0].provider_payment_id == "pay_captured"
    assert observations[0].provider_order_id == "order_captured"
    assert observations[0].bin_bucket == "411111"


def test_failed_enqueue_keeps_committed_webhook_available_for_dispatch(
    client, session_factory, monkeypatch
):
    def unavailable_broker(_webhook_id: int) -> None:
        raise ConnectionError("Redis is temporarily unavailable")

    monkeypatch.setattr("leakproof.api.app.process_webhook.delay", unavailable_broker)

    response = post_webhook(client, payment_failed("pay_broker_down"), "rzp_evt_broker_down")

    assert response.status_code == 200
    assert response.json()["duplicate"] is False
    with session_factory() as session:
        stored = session.get(WebhookEvent, response.json()["webhook_id"])
        assert stored is not None
        assert stored.processed_at is None
        assert stored.processing_attempts == 0


def test_bad_signature_is_rejected_before_persistence(client, session_factory):
    response = client.post(
        "/webhooks/razorpay",
        json=payment_failed("pay_bad"),
        headers={"x-razorpay-signature": "wrong"},
    )

    assert response.status_code == 401
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WebhookEvent)) == 0
