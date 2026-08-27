from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from leakproof.audit.timeline import append_event
from leakproof.config import get_measurement_config
from leakproof.models.db import (
    Action,
    Customer,
    Merchant,
    RecoveryAttribution,
    RecoveryCase,
)
from leakproof.models.domain import Arm, CaseOutcome, CaseState, LeakType


@dataclass(frozen=True)
class NormalizedSignal:
    merchant_id: str
    customer_id: str
    leak_type: LeakType
    entity_type: str
    entity_id: str
    entity_root_id: str | None
    amount_at_risk: int
    currency: str
    evidence: dict
    occurred_at: datetime


def new_id(prefix: str) -> str:
    timestamp = int(datetime.now(UTC).timestamp() * 1000)
    return f"{prefix}_{timestamp:012x}{secrets.token_hex(6)}"


def dedupe_key(signal: NormalizedSignal) -> str:
    if signal.leak_type == LeakType.PAYMENT_FAILURE:
        root = signal.entity_root_id or signal.entity_id
        return f"pf:{signal.customer_id}:{root}"
    if signal.leak_type == LeakType.CHECKOUT_ABANDON:
        return f"ca:{signal.entity_id}"
    if signal.leak_type == LeakType.SUBSCRIPTION_HALT:
        cycle = signal.evidence.get("cycle_number", "unknown")
        return f"sh:{signal.entity_id}:{cycle}"
    if signal.leak_type == LeakType.INVOICE_OVERDUE:
        return f"io:{signal.entity_id}"
    if signal.leak_type == LeakType.MANDATE_BROKEN:
        return f"mb:{signal.entity_id}"
    raise ValueError(f"unsupported leak type: {signal.leak_type}")


def amount_band(amount_paise: int) -> str:
    bands = get_measurement_config().holdout.amount_bands_paise
    if amount_paise <= bands.low_max:
        return "LOW"
    if amount_paise <= bands.medium_max:
        return "MEDIUM"
    return "HIGH"


@dataclass(frozen=True)
class ArmAssignment:
    arm: Arm
    bucket: int
    fraction: float
    seed: int
    stratum: str


def assigned_arm(
    merchant_id: str,
    customer_id: str,
    leak_type: LeakType,
    amount_at_risk: int,
) -> ArmAssignment:
    """Deterministically randomize within leak-type and amount-band strata."""
    holdout = get_measurement_config().holdout
    band = amount_band(amount_at_risk)
    stratum = f"{leak_type.value}:{band}"
    value = f"{merchant_id}:{customer_id}:{stratum}:{holdout.seed}".encode()
    bucket = int.from_bytes(hashlib.sha256(value).digest()[:8], "big") % 10_000
    threshold = round(holdout.fraction * 10_000)
    return ArmAssignment(
        arm=Arm.HOLDOUT if bucket < threshold else Arm.TREATMENT,
        bucket=bucket,
        fraction=holdout.fraction,
        seed=holdout.seed,
        stratum=stratum,
    )


def attribution_window(leak_type: LeakType) -> timedelta:
    config = get_measurement_config().attribution
    try:
        return timedelta(days=config.windows_days[leak_type.value])
    except KeyError as exc:
        raise ValueError(f"missing attribution window for {leak_type.value}") from exc


def ensure_principals(session: Session, signal: NormalizedSignal) -> None:
    if session.get(Merchant, signal.merchant_id) is None:
        merchant = Merchant(id=signal.merchant_id, name=signal.merchant_id, policy={})
        try:
            with session.begin_nested():
                session.add(merchant)
                session.flush()
        except IntegrityError:
            if session.get(Merchant, signal.merchant_id) is None:
                raise
    if session.get(Customer, signal.customer_id) is None:
        customer = Customer(id=signal.customer_id, merchant_id=signal.merchant_id)
        try:
            with session.begin_nested():
                session.add(customer)
                session.flush()
        except IntegrityError:
            if session.get(Customer, signal.customer_id) is None:
                raise


def record_signal(session: Session, signal: NormalizedSignal) -> tuple[RecoveryCase, bool]:
    """Create one case per dedupe key; every accepted signal becomes one event."""
    ensure_principals(session, signal)
    key = dedupe_key(signal)
    case = session.scalar(
        select(RecoveryCase).where(
            RecoveryCase.merchant_id == signal.merchant_id,
            RecoveryCase.dedupe_key == key,
        )
    )
    created = case is None
    if created:
        assignment = assigned_arm(
            signal.merchant_id,
            signal.customer_id,
            signal.leak_type,
            signal.amount_at_risk,
        )
        simulation = signal.evidence.get("simulation") or {}
        case = RecoveryCase(
            id=new_id("case"),
            merchant_id=signal.merchant_id,
            customer_id=signal.customer_id,
            leak_type=signal.leak_type.value,
            entity_type=signal.entity_type,
            entity_id=signal.entity_id,
            dedupe_key=key,
            batch_run_id=simulation.get("run_id"),
            amount_band=amount_band(signal.amount_at_risk),
            amount_at_risk=signal.amount_at_risk,
            currency=signal.currency,
            state=CaseState.DETECTED.value,
            arm=assignment.arm.value,
            detected_at=signal.occurred_at,
            attribution_until=signal.occurred_at + attribution_window(signal.leak_type),
        )
        try:
            with session.begin_nested():
                session.add(case)
                session.flush()
        except IntegrityError:
            # A savepoint preserves the inbox row lock and its outer transaction.
            case = session.scalar(
                select(RecoveryCase).where(
                    RecoveryCase.merchant_id == signal.merchant_id,
                    RecoveryCase.dedupe_key == key,
                )
            )
            if case is None:
                raise
            created = False

    assert case is not None
    payload = {
        "leak_type": signal.leak_type.value,
        "entity_id": signal.entity_id,
        "entity_root_id": signal.entity_root_id,
        "amount_at_risk": signal.amount_at_risk,
        "currency": signal.currency,
        "evidence": signal.evidence,
    }
    if created:
        payload["case"] = {
            "id": case.id,
            "merchant_id": case.merchant_id,
            "customer_id": case.customer_id,
            "leak_type": case.leak_type,
            "entity_type": case.entity_type,
            "entity_id": case.entity_id,
            "dedupe_key": case.dedupe_key,
            "batch_run_id": case.batch_run_id,
            "amount_band": case.amount_band,
            "amount_at_risk": case.amount_at_risk,
            "currency": case.currency,
            "state": case.state,
            "arm": case.arm,
            "detected_at": case.detected_at.isoformat(),
            "attribution_until": case.attribution_until.isoformat(),
        }
    append_event(
        session,
        case,
        kind="DETECTED" if created else "SIGNAL",
        payload=payload,
        actor="sensor",
    )
    if created:
        append_event(
            session,
            case,
            kind="ASSIGNED",
            payload={
                "arm": assignment.arm.value,
                "bucket": assignment.bucket,
                "random_value": assignment.bucket / 10_000,
                "holdout_fraction": assignment.fraction,
                "seed": assignment.seed,
                "stratum": assignment.stratum,
                "stratify_by": ["leak_type", "amount_band"],
            },
            actor="holdout_allocator",
        )
    return case, created


@dataclass(frozen=True)
class PaidSignal:
    merchant_id: str
    customer_id: str | None
    entity_id: str
    entity_root_id: str | None
    amount_paise: int
    currency: str
    evidence: dict
    occurred_at: datetime


def record_paid_signal(session: Session, signal: PaidSignal) -> RecoveryCase | None:
    """Stop a paid case and persist pre-declared, last-touch attribution when eligible."""
    exact = [RecoveryCase.entity_id == signal.entity_id]
    if signal.entity_root_id and signal.customer_id:
        exact.append(RecoveryCase.dedupe_key == f"pf:{signal.customer_id}:{signal.entity_root_id}")
    candidates = list(
        session.scalars(
            select(RecoveryCase)
            .where(RecoveryCase.merchant_id == signal.merchant_id)
            .where(
                or_(
                    *exact,
                    RecoveryCase.customer_id == signal.customer_id
                    if signal.customer_id
                    else RecoveryCase.id == "__no_customer__",
                )
            )
            .order_by(RecoveryCase.detected_at.desc())
            .with_for_update()
        )
    )
    tolerance = get_measurement_config().attribution.amount_tolerance_pct / 100
    case = None
    matched_by = ""
    for candidate in candidates:
        entity_match = candidate.entity_id == signal.entity_id or (
            signal.entity_root_id is not None
            and candidate.dedupe_key == f"pf:{signal.customer_id}:{signal.entity_root_id}"
        )
        amount_match = abs(candidate.amount_at_risk - signal.amount_paise) <= round(
            candidate.amount_at_risk * tolerance
        )
        customer_amount_match = (
            signal.customer_id is not None
            and candidate.customer_id == signal.customer_id
            and amount_match
        )
        if entity_match or customer_amount_match:
            case = candidate
            matched_by = "entity_id" if entity_match else "customer_id_and_amount_within_1pct"
            break
    if case is None:
        return None
    if case.outcome == CaseOutcome.RECOVERED.value:
        return case

    pending = list(
        session.scalars(
            select(Action).where(
                Action.case_id == case.id,
                Action.status == "pending",
            )
        )
    )
    for action in pending:
        action.status = "cancelled"
    paid_at = signal.occurred_at
    detected_at = case.detected_at
    attribution_until = case.attribution_until
    if detected_at.tzinfo is None:
        detected_at = detected_at.replace(tzinfo=UTC)
    if attribution_until.tzinfo is None:
        attribution_until = attribution_until.replace(tzinfo=UTC)
    if paid_at.tzinfo is None:
        paid_at = paid_at.replace(tzinfo=UTC)
    within_window = detected_at <= paid_at <= attribution_until
    last_touch = session.scalar(
        select(Action)
        .where(
            Action.case_id == case.id,
            Action.executed_at.is_not(None),
            Action.executed_at <= signal.occurred_at,
        )
        .order_by(Action.executed_at.desc(), Action.step_index.desc())
        .limit(1)
    )
    organic = case.arm == Arm.HOLDOUT.value or last_touch is None
    append_event(
        session,
        case,
        kind="VERIFYING",
        payload={
            "signal": "payment_paid",
            "entity_id": signal.entity_id,
            "entity_root_id": signal.entity_root_id,
            "amount_paise": signal.amount_paise,
            "currency": signal.currency,
            "evidence": signal.evidence,
            "match_rule": matched_by,
            "within_attribution_window": within_window,
        },
        actor="sensor",
    )
    attribution = None
    if within_window:
        attribution = RecoveryAttribution(
            case_id=case.id,
            payment_entity_id=signal.entity_id,
            amount_paise=signal.amount_paise,
            matched_by=matched_by,
            credit_rule=get_measurement_config().attribution.credit_rule,
            credited_action_id=last_touch.id if last_touch else None,
            credited_action_type=last_touch.action_type if last_touch else None,
            touch_at=last_touch.executed_at if last_touch else None,
            organic=organic,
            paid_at=signal.occurred_at,
        )
        session.add(attribution)
    case.outcome = CaseOutcome.RECOVERED.value
    case.closed_at = signal.occurred_at
    append_event(
        session,
        case,
        kind="CLOSED",
        payload={
            "outcome": "RECOVERED",
            "cancelled_action_ids": [action.id for action in pending],
            "attribution": (
                {
                    "credited": True,
                    "credit_rule": "last_touch",
                    "action_id": last_touch.id if last_touch else None,
                    "action_type": last_touch.action_type if last_touch else None,
                    "organic": organic,
                    "matched_by": matched_by,
                }
                if attribution is not None
                else {
                    "credited": False,
                    "reason": "outside_predeclared_window",
                    "matched_by": matched_by,
                }
            ),
        },
        actor="attribution_verifier",
    )
    session.flush()
    return case
