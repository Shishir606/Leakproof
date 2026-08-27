from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from leakproof.models.db import WebhookEvent
from leakproof.sensors.normalizer import normalize_razorpay, normalize_razorpay_paid
from leakproof.services import record_paid_signal, record_signal


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
            case = record_paid_signal(session, paid)
            case_id = case.id if case is not None else None
        elif signal is not None:
            case, _ = record_signal(session, signal)
            case_id = case.id
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
