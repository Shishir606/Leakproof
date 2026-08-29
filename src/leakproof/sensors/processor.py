from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from leakproof.audit.timeline import append_event
from leakproof.demo.contracts import DemoSessionState, live_case_dedupe_key
from leakproof.diagnosis import refresh_payment_diagnosis
from leakproof.models.db import DemoSession, RecoveryCase, WebhookEvent
from leakproof.models.domain import Arm, LeakType
from leakproof.sensors.normalizer import normalize_razorpay, normalize_razorpay_paid
from leakproof.services import NormalizedSignal, PaidSignal, record_paid_signal, record_signal


def _demo_for_order(
    session: Session, merchant_id: str, order_id: str | None
) -> DemoSession | None:
    if not order_id:
        return None
    return session.scalar(
        select(DemoSession)
        .where(
            DemoSession.merchant_id == merchant_id,
            DemoSession.razorpay_order_id == order_id,
        )
        .with_for_update()
    )


def _validate_demo_amount(demo: DemoSession, amount_paise: int, currency: str) -> None:
    if demo.amount_paise != amount_paise or demo.currency != currency:
        raise ValueError("Razorpay webhook amount or currency does not match the demo order")


def _bind_live_failure(signal: NormalizedSignal, demo: DemoSession) -> NormalizedSignal:
    _validate_demo_amount(demo, signal.amount_at_risk, signal.currency)
    return replace(
        signal,
        customer_id=demo.customer_id,
        dedupe_key_override=live_case_dedupe_key(demo.id, demo.razorpay_order_id),
        arm_override=Arm.TREATMENT,
        evidence={
            **signal.evidence,
            "source": "razorpay_webhook",
            "session_id": demo.id,
        },
    )


def _bind_live_paid(signal: PaidSignal, demo: DemoSession) -> PaidSignal:
    _validate_demo_amount(demo, signal.amount_paise, signal.currency)
    return replace(signal, customer_id=demo.customer_id)


def _promote_abandonment_case(
    session: Session, case: RecoveryCase, signal: NormalizedSignal
) -> None:
    if case.leak_type != LeakType.CHECKOUT_ABANDON.value:
        return
    previous_type = case.leak_type
    case.leak_type = LeakType.PAYMENT_FAILURE.value
    case.entity_type = signal.entity_type
    case.entity_id = signal.entity_id
    case.amount_at_risk = signal.amount_at_risk
    case.currency = signal.currency
    append_event(
        session,
        case,
        kind="RECLASSIFIED",
        payload={
            "from_leak_type": previous_type,
            "to_leak_type": LeakType.PAYMENT_FAILURE.value,
            "entity_type": signal.entity_type,
            "entity_id": signal.entity_id,
            "amount_at_risk": signal.amount_at_risk,
            "currency": signal.currency,
            "reason": "razorpay_payment_failure_takes_precedence",
        },
        actor="razorpay_reconciler",
    )
    refresh_payment_diagnosis(session, case, signal.evidence)


def _previous_paid_signal(
    session: Session, merchant_id: str, order_id: str
) -> PaidSignal | None:
    """Find webhook truth already processed before a delayed failure event."""
    candidates: list[PaidSignal] = []
    events = session.scalars(
        select(WebhookEvent).where(
            WebhookEvent.merchant_id == merchant_id,
            WebhookEvent.provider == "razorpay",
            WebhookEvent.event_type.in_(["payment.captured", "order.paid"]),
            WebhookEvent.processed_at.is_not(None),
        )
    )
    for webhook in events:
        paid = normalize_razorpay_paid(merchant_id, webhook.payload)
        if paid is not None and (paid.entity_root_id == order_id or paid.entity_id == order_id):
            candidates.append(paid)
    return max(candidates, key=lambda item: item.occurred_at, default=None)


def process_stored_webhook(session: Session, webhook_id: int) -> str | None:
    event = session.scalar(
        select(WebhookEvent).where(WebhookEvent.id == webhook_id).with_for_update()
    )
    if event is None:
        raise LookupError(webhook_id)
    if event.processed_at is not None:
        return None

    event.processing_attempts += 1
    try:
        paid = normalize_razorpay_paid(event.merchant_id, event.payload)
        signal = normalize_razorpay(event.merchant_id, event.payload)
        case_id: str | None = None
        if paid is not None:
            order_id = paid.entity_root_id or paid.entity_id
            demo = _demo_for_order(session, event.merchant_id, order_id)
            if demo is not None:
                paid = _bind_live_paid(paid, demo)
            case = record_paid_signal(session, paid)
            case_id = case.id if case is not None else None
            if demo is not None and DemoSessionState(demo.state) != DemoSessionState.EXPIRED:
                demo.state = DemoSessionState.RECOVERED.value
                demo.updated_at = datetime.now(UTC)
        elif signal is not None:
            demo = _demo_for_order(session, event.merchant_id, signal.entity_root_id)
            prior_paid = None
            if demo is not None:
                signal = _bind_live_failure(signal, demo)
                if DemoSessionState(demo.state) == DemoSessionState.EXPIRED:
                    signal = None
                elif DemoSessionState(demo.state) == DemoSessionState.RECOVERED:
                    prior_paid = _previous_paid_signal(
                        session, event.merchant_id, demo.razorpay_order_id
                    )
                    if prior_paid is None or prior_paid.occurred_at < signal.occurred_at:
                        signal = None
            if signal is None:
                event.processed_at = datetime.now(UTC)
                event.last_error = None
                session.commit()
                return None
            case, _ = record_signal(session, signal)
            _promote_abandonment_case(session, case, signal)
            case_id = case.id
            if demo is not None:
                if DemoSessionState(demo.state) != DemoSessionState.RECOVERED:
                    demo.state = DemoSessionState.AT_RISK.value
                    demo.updated_at = datetime.now(UTC)
                elif prior_paid is not None:
                    closed = record_paid_signal(session, _bind_live_paid(prior_paid, demo))
                    case_id = closed.id if closed is not None else case_id
        event.processed_at = datetime.now(UTC)
        event.last_error = None
        session.commit()
        return case_id
    except Exception as exc:
        session.rollback()
        failed = session.get(WebhookEvent, webhook_id)
        if failed is not None:
            failed.processing_attempts += 1
            failed.last_error = str(exc)[:2000]
            session.commit()
        raise
