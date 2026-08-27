from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from leakproof.models.db import WebhookEvent


class InvalidWebhookSignature(ValueError):
    pass


@dataclass(frozen=True)
class IngestedWebhook:
    id: int
    duplicate: bool


def verify_razorpay_signature(body: bytes, signature: str, secret: str) -> None:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        raise InvalidWebhookSignature("invalid Razorpay webhook signature")


def provider_event_key(payload: dict, header_event_id: str | None) -> str:
    if header_event_id:
        return header_event_id
    event_type = str(payload.get("event", "unknown"))
    entity_ids: list[str] = []
    for envelope in payload.get("payload", {}).values():
        entity = envelope.get("entity", {}) if isinstance(envelope, dict) else {}
        if entity.get("id"):
            entity_ids.append(str(entity["id"]))
    stable = json.dumps(
        {
            "event": event_type,
            "entities": sorted(entity_ids),
            "created_at": payload.get("created_at"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "fp_" + hashlib.sha256(stable.encode()).hexdigest()


def persist_webhook(
    session: Session,
    *,
    merchant_id: str,
    payload: dict,
    header_event_id: str | None,
) -> IngestedWebhook:
    key = provider_event_key(payload, header_event_id)
    event = WebhookEvent(
        merchant_id=merchant_id,
        provider="razorpay",
        provider_event_key=key,
        event_type=str(payload.get("event", "unknown")),
        payload=payload,
        signature_verified=True,
    )
    session.add(event)
    try:
        session.commit()  # durability boundary: commit before HTTP 200 and enqueue
        return IngestedWebhook(id=event.id, duplicate=False)
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(WebhookEvent).where(
                WebhookEvent.merchant_id == merchant_id,
                WebhookEvent.provider == "razorpay",
                WebhookEvent.provider_event_key == key,
            )
        )
        if existing is None:
            raise
        return IngestedWebhook(id=existing.id, duplicate=True)
