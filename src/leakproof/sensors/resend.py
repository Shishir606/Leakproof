from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from leakproof.models.db import EmailDelivery, EmailDeliveryEvent, WebhookEvent

_TYPE_TO_STATUS = {
    "email.sent": "sent",
    "email.delivered": "delivered",
    "email.clicked": "clicked",
    "email.bounced": "bounced",
    "email.complained": "complained",
    "email.failed": "failed",
}
_TIE_BREAK = {
    "sent": 0,
    "delivered": 1,
    "clicked": 2,
    "failed": 3,
    "bounced": 4,
    "complained": 5,
}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def latest_delivery_status(
    session: Session, provider_email_id: str, *, default: str = "sent"
) -> str:
    events = list(
        session.scalars(
            select(EmailDeliveryEvent).where(
                EmailDeliveryEvent.provider_email_id == provider_email_id
            )
        )
    )
    if not events:
        return default
    latest = max(
        events,
        key=lambda item: (
            _utc(item.occurred_at),
            _TIE_BREAK[_TYPE_TO_STATUS[item.event_type]],
            item.provider_event_id,
        ),
    )
    return _TYPE_TO_STATUS[latest.event_type]


def process_stored_resend_webhook(session: Session, webhook_id: int) -> str | None:
    """Persist one delivery event and recompute state from event time, not arrival order."""
    webhook = session.scalar(
        select(WebhookEvent).where(WebhookEvent.id == webhook_id).with_for_update()
    )
    if webhook is None:
        raise LookupError(webhook_id)
    if webhook.provider != "resend":
        raise ValueError("webhook does not belong to Resend")
    if webhook.processed_at is not None:
        return None

    webhook.processing_attempts += 1
    try:
        payload = webhook.payload
        event_type = str(payload["type"])
        status = _TYPE_TO_STATUS[event_type]
        provider_email_id = str(payload["data"]["email_id"])
        occurred_at = datetime.fromisoformat(str(payload["created_at"]).replace("Z", "+00:00"))
        delivery = session.scalar(
            select(EmailDelivery)
            .where(EmailDelivery.provider_email_id == provider_email_id)
            .with_for_update()
        )
        session.add(
            EmailDeliveryEvent(
                provider_email_id=provider_email_id,
                provider_event_id=webhook.provider_event_key,
                event_type=event_type,
                safe_payload={"status": status},
                occurred_at=_utc(occurred_at),
            )
        )
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            duplicate = session.get(WebhookEvent, webhook_id)
            if duplicate is not None:
                duplicate.processed_at = datetime.now(UTC)
                duplicate.last_error = None
                session.commit()
            return delivery.case_id if delivery is not None else None

        if delivery is not None:
            delivery.status = latest_delivery_status(
                session, provider_email_id, default=delivery.status
            )
            delivery.updated_at = datetime.now(UTC)
        webhook.processed_at = datetime.now(UTC)
        webhook.last_error = None
        session.commit()
        return delivery.case_id if delivery is not None else None
    except Exception as exc:
        session.rollback()
        failed = session.get(WebhookEvent, webhook_id)
        if failed is not None:
            failed.processing_attempts += 1
            failed.last_error = str(exc)[:2000]
            session.commit()
        raise
