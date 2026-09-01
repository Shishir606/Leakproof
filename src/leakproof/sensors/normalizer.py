from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class NormalizedPaymentAttempt:
    merchant_id: str
    provider_event_key: str
    attempt_key: str
    provider_payment_id: str | None
    provider_order_id: str | None
    observed_at: datetime
    outcome: str
    method: str
    issuer: str
    bin_bucket: str
    checkout_step: str
    checkout_version: str
    error_reason: str


def _safe_dimension(value: object) -> str:
    if value is None or value == "":
        return "unknown"
    return str(value)[:160]


def normalize_razorpay_attempt(
    merchant_id: str, payload: dict, provider_event_key: str
) -> NormalizedPaymentAttempt | None:
    """Extract non-customer aggregate facts from a verified payment webhook."""
    event = payload.get("event")
    if event not in {"payment.failed", "payment.captured", "order.paid"}:
        return None
    payment = _entity(payload, "payment")
    order = _entity(payload, "order")
    entity = payment if payment else order
    payment_id = str(payment["id"]) if payment.get("id") else None
    order_id_value = payment.get("order_id") or order.get("id")
    order_id = str(order_id_value) if order_id_value else None
    outcome = "failure" if event == "payment.failed" else "success"
    if outcome == "success" and order_id:
        attempt_key = f"success:order:{order_id}"
    elif payment_id:
        attempt_key = f"payment:{payment_id}"
    elif order_id:
        attempt_key = f"order:{order_id}"
    else:
        attempt_key = f"event:{provider_event_key}"
    card = payment.get("card") if isinstance(payment.get("card"), dict) else {}
    notes = entity.get("notes") if isinstance(entity.get("notes"), dict) else {}
    return NormalizedPaymentAttempt(
        merchant_id=merchant_id,
        provider_event_key=provider_event_key,
        attempt_key=attempt_key,
        provider_payment_id=payment_id,
        provider_order_id=order_id,
        observed_at=_occurred_at(payload, entity),
        outcome=outcome,
        method=_safe_dimension(payment.get("method")),
        issuer=_safe_dimension(payment.get("bank") or card.get("issuer")),
        bin_bucket=_safe_dimension(card.get("iin") or card.get("bin")),
        checkout_step=_safe_dimension(payment.get("error_step") or notes.get("checkout_step")),
        checkout_version=_safe_dimension(notes.get("checkout_version")),
        error_reason=_safe_dimension(payment.get("error_reason")),
    )


def normalize_razorpay(merchant_id: str, payload: dict) -> NormalizedSignal | None:
    event = payload.get("event")
    if event == "payment.failed":
        entity = _entity(payload, "payment")
        card = entity.get("card") if isinstance(entity.get("card"), dict) else {}
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
                    "method",
                )
                if entity.get(key) is not None
            }
            | {
                key: value
                for key, value in {
                    "issuer": entity.get("bank") or card.get("issuer"),
                    "bin": card.get("iin") or card.get("bin"),
                    "checkout_step": entity.get("error_step"),
                }.items()
                if value is not None
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
