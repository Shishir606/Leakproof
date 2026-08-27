from __future__ import annotations

from datetime import UTC, datetime

from leakproof.models.domain import LeakType
from leakproof.services import NormalizedSignal, PaidSignal


def _entity(payload: dict, name: str) -> dict:
    return payload.get("payload", {}).get(name, {}).get("entity", {})


def _occurred_at(payload: dict, entity: dict) -> datetime:
    value = entity.get("created_at") or payload.get("created_at")
    return (
        datetime.fromtimestamp(value, UTC) if isinstance(value, int | float) else datetime.now(UTC)
    )


def normalize_razorpay(merchant_id: str, payload: dict) -> NormalizedSignal | None:
    event = payload.get("event")
    if event == "payment.failed":
        entity = _entity(payload, "payment")
        entity_id = str(entity["id"])
        order_id = entity.get("order_id")
        customer_id = entity.get("customer_id") or entity.get("notes", {}).get("customer_id")
        customer_id = str(customer_id or f"anonymous:{order_id or entity_id}")
        return NormalizedSignal(
            merchant_id=merchant_id,
            customer_id=customer_id,
            leak_type=LeakType.PAYMENT_FAILURE,
            entity_type="payment",
            entity_id=entity_id,
            entity_root_id=str(order_id) if order_id else None,
            amount_at_risk=int(entity.get("amount", 0)),
            currency=str(entity.get("currency", "INR")),
            evidence={
                key: entity.get(key)
                for key in (
                    "error_code",
                    "error_description",
                    "error_source",
                    "error_step",
                    "error_reason",
                )
                if entity.get(key) is not None
            },
            occurred_at=_occurred_at(payload, entity),
        )

    mapping = {
        "subscription.halted": ("subscription", LeakType.SUBSCRIPTION_HALT),
        "subscription.pending": ("subscription", LeakType.SUBSCRIPTION_HALT),
    }
    if event in mapping:
        envelope, leak_type = mapping[event]
        entity = _entity(payload, envelope)
        entity_id = str(entity["id"])
        customer_id = str(entity.get("customer_id") or f"anonymous:{entity_id}")
        return NormalizedSignal(
            merchant_id=merchant_id,
            customer_id=customer_id,
            leak_type=leak_type,
            entity_type=envelope,
            entity_id=entity_id,
            entity_root_id=None,
            amount_at_risk=int(entity.get("current_end_amount", entity.get("amount", 0))),
            currency=str(entity.get("currency", "INR")),
            evidence={"status": entity.get("status"), "cycle_number": entity.get("paid_count", 0)},
            occurred_at=_occurred_at(payload, entity),
        )

    # Success events are normalized separately so they cannot create leak cases.
    return None


def normalize_razorpay_paid(merchant_id: str, payload: dict) -> PaidSignal | None:
    event = payload.get("event")
    if event == "payment.captured":
        entity = _entity(payload, "payment")
        entity_id = str(entity["id"])
        order_id = entity.get("order_id")
        customer_id = entity.get("customer_id") or entity.get("notes", {}).get("customer_id")
        return PaidSignal(
            merchant_id=merchant_id,
            customer_id=str(customer_id) if customer_id else None,
            entity_id=entity_id,
            entity_root_id=str(order_id) if order_id else None,
            amount_paise=int(entity.get("amount", 0)),
            currency=str(entity.get("currency", "INR")),
            evidence={"provider_event": event, "status": entity.get("status")},
            occurred_at=_occurred_at(payload, entity),
        )
    if event == "order.paid":
        entity = _entity(payload, "order")
        entity_id = str(entity["id"])
        customer_id = entity.get("customer_id") or entity.get("notes", {}).get("customer_id")
        return PaidSignal(
            merchant_id=merchant_id,
            customer_id=str(customer_id) if customer_id else None,
            entity_id=entity_id,
            entity_root_id=entity_id,
            amount_paise=int(entity.get("amount_paid", entity.get("amount", 0))),
            currency=str(entity.get("currency", "INR")),
            evidence={"provider_event": event, "status": entity.get("status")},
            occurred_at=_occurred_at(payload, entity),
        )
    return None
