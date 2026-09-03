"""Transactional correlation/settlement foundation, shared by later provider adapters.

Callers supply verified, explicit relationships. This module never makes provider
calls, dispatches contact, or infers an invoice from a customer or subscription.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from leakproof.audit.timeline import append_event
from leakproof.config import get_measurement_config
from leakproof.models.db import (
    Action,
    DemoSession,
    ProviderEntity,
    ProviderObligation,
    RecoveryAttribution,
    RecoveryCase,
    Settlement,
)
from leakproof.models.domain import Arm, CaseOutcome, LeakType
from leakproof.models.resources import (
    LEAK_PRECEDENCE,
    EntityRef,
    EntityStateSignal,
    EntityType,
    ObligationRef,
    ProviderScope,
    RecoverySignal,
    RiskSignal,
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _lock_scope(session: Session, scope: ProviderScope) -> None:
    # Correlation can touch two obligations; one namespace lock avoids lock inversion
    # during late order->invoice attachment and concurrent settlement delivery.
    if session.bind.dialect.name == "postgresql":
        key = int(scope.identity(EntityRef(entity_type="order", entity_id="order_lock"))[4:19], 16)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def get_obligation(
    session: Session, scope: ProviderScope, ref: ObligationRef, currency: str
) -> ProviderObligation:
    from leakproof.services import ensure_merchant

    _lock_scope(session, scope)
    ensure_merchant(session, scope.merchant_id)
    identity = scope.identity(ref)
    row = session.get(ProviderObligation, identity, populate_existing=True)
    if row is None:
        row = ProviderObligation(
            id=identity,
            merchant_id=scope.merchant_id,
            provider=scope.provider,
            mode=scope.mode,
            entity_type=ref.entity_type,
            provider_entity_id=ref.entity_id,
            currency=currency,
        )
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            row = session.get(ProviderObligation, identity, populate_existing=True)
            if row is None:
                raise
    while row.alias_of:
        row = session.get(ProviderObligation, row.alias_of, populate_existing=True)
    if row.currency != currency:
        raise ValueError("obligation currency mismatch")
    return row


def find_entity(session: Session, scope: ProviderScope, ref: EntityRef) -> ProviderEntity | None:
    return session.scalar(
        select(ProviderEntity).where(
            ProviderEntity.merchant_id == scope.merchant_id,
            ProviderEntity.provider == scope.provider,
            ProviderEntity.mode == scope.mode,
            ProviderEntity.entity_type == ref.entity_type,
            ProviderEntity.provider_entity_id == ref.entity_id,
        )
    )


def _quarantine(session: Session, *obligations: ProviderObligation) -> None:
    for obligation in obligations:
        obligation.reconciliation_required = True
        if obligation.case_id:
            case = session.get(RecoveryCase, obligation.case_id)
            cancelled = _cancel_actions(session, case)
            append_event(
                session,
                case,
                kind="RECONCILIATION_REQUIRED",
                actor="provider_reconciler",
                payload={
                    "reason": "conflicting_obligation_owners",
                    "cancelled_action_ids": cancelled,
                },
            )


def _attach_invoice(
    session: Session, old: ProviderObligation, canonical: ProviderObligation
) -> None:
    if old.id == canonical.id:
        return
    if old.entity_type != "order" or canonical.entity_type != "invoice":
        _quarantine(session, old, canonical)
        return
    if old.case_id and canonical.case_id and old.case_id != canonical.case_id:
        _quarantine(session, old, canonical)
        return
    if old.currency != canonical.currency:
        raise ValueError("relationship currency mismatch")
    if old.case_id:
        owner = old.case_id
        old.case_id = None
        session.flush()  # release unique case ownership before moving it
        canonical.case_id = owner
        for name in (
            "baseline_paid_paise",
            "detected_due_paise",
            "detected_at",
            "outstanding_paise",
            "recovered_paise",
        ):
            setattr(canonical, name, getattr(old, name))
        append_event(
            session,
            session.get(RecoveryCase, owner),
            kind="OBLIGATION_ATTACHED",
            actor="provider_reconciler",
            payload={"entity_type": "invoice"},
        )
    canonical.settled_at = canonical.settled_at or old.settled_at
    for settlement in session.scalars(select(Settlement).where(Settlement.obligation_id == old.id)):
        settlement.obligation_id = canonical.id
    for entity in session.scalars(
        select(ProviderEntity).where(ProviderEntity.obligation_id == old.id)
    ):
        entity.obligation_id = canonical.id
    old.alias_of = canonical.id
    old.recovered_paise = 0
    session.flush()


def register_entity(
    session: Session,
    scope: ProviderScope,
    ref: EntityRef,
    *,
    session_id: str | None = None,
    root: EntityRef | None = None,
    obligation: ProviderObligation | None = None,
    role: str = "related",
) -> ProviderEntity:
    from leakproof.services import ensure_merchant

    _lock_scope(session, scope)
    ensure_merchant(session, scope.merchant_id)
    if session_id:
        demo = session.get(DemoSession, session_id)
        if not demo or demo.merchant_id != scope.merchant_id or demo.provider_mode != scope.mode:
            raise ValueError("provider entity session scope mismatch")
    if obligation and (obligation.merchant_id, obligation.provider, obligation.mode) != (
        scope.merchant_id,
        scope.provider,
        scope.mode,
    ):
        raise ValueError("provider entity obligation scope mismatch")
    # Parents can own many unpaid cycles: never bind them to just one invoice.
    if ref.entity_type in {EntityType.SUBSCRIPTION, EntityType.TOKEN}:
        obligation = None
        root = None
    row = find_entity(session, scope, ref)
    if row is None:
        row = ProviderEntity(
            merchant_id=scope.merchant_id,
            provider=scope.provider,
            mode=scope.mode,
            session_id=session_id,
            entity_type=ref.entity_type,
            provider_entity_id=ref.entity_id,
            role=role,
        )
        session.add(row)
    if root:
        if row.root_entity_id and (row.root_entity_type, row.root_entity_id) != (
            root.entity_type,
            root.entity_id,
        ):
            # An order root can be refined to an invoice only via the explicit attach operation.
            if row.root_entity_type != "order" or root.entity_type != "invoice":
                raise ValueError("conflicting provider root relationship")
        row.root_entity_type, row.root_entity_id = root.entity_type, root.entity_id
    if obligation:
        if row.obligation_id and row.obligation_id != obligation.id:
            old = session.get(ProviderObligation, row.obligation_id)
            _attach_invoice(session, old, obligation)
            if old.reconciliation_required or obligation.reconciliation_required:
                return row
        row.obligation_id = obligation.id
    session.flush()
    return row


def _cancel_actions(session: Session, case: RecoveryCase) -> list[str]:
    pending = list(
        session.scalars(select(Action).where(Action.case_id == case.id, Action.status == "pending"))
    )
    for action in pending:
        action.status = "cancelled"
    return [action.id for action in pending]


def record_risk(
    session: Session, signal: RiskSignal, *, legacy_signal=None, session_id: str | None = None
) -> tuple[RecoveryCase | None, bool]:
    from leakproof.services import NormalizedSignal, record_signal

    if signal.obligation is None:
        register_entity(
            session, signal.scope, signal.entity, root=signal.root, session_id=session_id
        )
        return None, False  # provisional signals never contact a customer
    obligation = get_obligation(session, signal.scope, signal.obligation, signal.currency)
    register_entity(
        session,
        signal.scope,
        signal.obligation,
        obligation=obligation,
        root=signal.entity if signal.entity.entity_type == EntityType.SUBSCRIPTION else None,
    )
    register_entity(
        session,
        signal.scope,
        signal.entity,
        root=signal.root,
        obligation=obligation,
        session_id=session_id,
    )
    if obligation.reconciliation_required:
        return None, False
    case = session.get(RecoveryCase, obligation.case_id) if obligation.case_id else None
    if case is None and obligation.settled_at and legacy_signal is None:
        return None, False  # success-first cannot create retrospective recovered revenue
    if not signal.amount_due_paise:
        return case, False
    if case is not None and case.outcome == CaseOutcome.RECOVERED:
        return case, False
    if case is None:
        normalized = legacy_signal or NormalizedSignal(
            merchant_id=signal.scope.merchant_id,
            customer_id=signal.customer_id,
            leak_type=signal.leak_type,
            entity_type=signal.entity.entity_type,
            entity_id=signal.entity.entity_id,
            entity_root_id=signal.root.entity_id if signal.root else None,
            amount_at_risk=signal.amount_due_paise,
            currency=signal.currency,
            evidence={"source": signal.source},
            occurred_at=signal.occurred_at,
            dedupe_key_override=obligation.id,
        )
        case, created = record_signal(session, normalized, _resource_bound=True)
        obligation.case_id = case.id
        obligation.detected_at = signal.occurred_at
        obligation.detected_due_paise = 0 if obligation.settled_at else signal.amount_due_paise
        obligation.outstanding_paise = signal.amount_due_paise
        obligation.baseline_paid_paise = signal.baseline_paid_paise
    else:
        created = False
        if legacy_signal:
            from dataclasses import replace

            record_signal(
                session,
                replace(legacy_signal, dedupe_key_override=case.dedupe_key),
                _resource_bound=True,
            )
    if (
        legacy_signal
        and case.leak_type == LeakType.CHECKOUT_ABANDON
        and signal.leak_type == LeakType.PAYMENT_FAILURE
    ):
        from leakproof.sensors.processor import promote_abandonment_case

        promote_abandonment_case(session, case, legacy_signal)
    if LEAK_PRECEDENCE[signal.leak_type] > LEAK_PRECEDENCE[LeakType(case.leak_type)]:
        previous = case.leak_type
        case.leak_type = signal.leak_type.value
        cancelled = _cancel_actions(session, case)
        append_event(
            session,
            case,
            kind="RECLASSIFIED",
            actor="provider_reconciler",
            payload={
                "from_leak_type": previous,
                "to_leak_type": case.leak_type,
                "cancelled_action_ids": cancelled,
                "reason": "same_obligation_precedence",
            },
        )
    if obligation.settled_at and case.outcome != CaseOutcome.RECOVERED:
        record_recovery(
            session,
            RecoverySignal(
                scope=signal.scope,
                entity=signal.entity,
                root=signal.root,
                obligation=signal.obligation,
                source=signal.source,
                occurred_at=_utc(obligation.settled_at),
                currency=signal.currency,
                amount_due_paise=0,
                settlement="full_settlement",
            ),
        )
    session.flush()
    return case, created


def record_state(session: Session, signal: EntityStateSignal) -> None:
    entity = register_entity(session, signal.scope, signal.entity, root=signal.root)
    if entity.state_observed_at and _utc(entity.state_observed_at) > signal.occurred_at:
        return
    entity.status, entity.state_observed_at = signal.state, signal.occurred_at
    # Service/authorization state never closes or credits an invoice.
    if signal.obligation and signal.currency:
        obligation = get_obligation(session, signal.scope, signal.obligation, signal.currency)
        if signal.amount_due_paise is not None and not obligation.settled_at:
            obligation.outstanding_paise = min(
                obligation.outstanding_paise
                if obligation.outstanding_paise is not None
                else signal.amount_due_paise,
                signal.amount_due_paise,
            )
        if obligation.case_id:
            append_event(
                session,
                session.get(RecoveryCase, obligation.case_id),
                kind="ENTITY_STATE",
                actor="provider_reconciler",
                payload={"state": signal.state, "amount_due_paise": signal.amount_due_paise},
            )
    session.flush()


def record_recovery(session: Session, signal: RecoverySignal) -> RecoveryCase | None:
    if signal.settlement == "authorization_repaired":
        record_state(
            session,
            EntityStateSignal(
                **signal.model_dump(
                    exclude={"kind", "leak_type", "payment_id", "amount_paise", "settlement"}
                ),
                state="authorization_repaired",
            ),
        )
        return None
    if signal.obligation is None:
        register_entity(session, signal.scope, signal.entity, root=signal.root)
        return None
    obligation = get_obligation(session, signal.scope, signal.obligation, signal.currency)
    register_entity(session, signal.scope, signal.entity, root=signal.root, obligation=obligation)
    case = session.get(RecoveryCase, obligation.case_id) if obligation.case_id else None
    if obligation.reconciliation_required:
        return None
    settlement = None
    new_settlement = False
    if signal.payment_id:
        settlement = session.scalar(
            select(Settlement).where(
                Settlement.merchant_id == signal.scope.merchant_id,
                Settlement.provider == signal.scope.provider,
                Settlement.mode == signal.scope.mode,
                Settlement.payment_id == signal.payment_id,
            )
        )
        if settlement:
            if (settlement.obligation_id, settlement.currency, settlement.amount_paise) != (
                obligation.id,
                signal.currency,
                signal.amount_paise,
            ):
                raise ValueError("payment settlement conflicts with its existing obligation")
        else:
            new_settlement = True
            settlement = Settlement(
                merchant_id=signal.scope.merchant_id,
                provider=signal.scope.provider,
                mode=signal.scope.mode,
                payment_id=signal.payment_id,
                obligation_id=obligation.id,
                amount_paise=signal.amount_paise,
                currency=signal.currency,
                occurred_at=signal.occurred_at,
                credited_paise=0,
            )
            session.add(settlement)
            session.flush()
            credit = 0
            if (
                case
                and obligation.detected_at
                and (
                    _utc(obligation.detected_at)
                    <= signal.occurred_at
                    <= _utc(case.attribution_until)
                )
            ):
                credit = min(
                    signal.amount_paise,
                    max(0, obligation.detected_due_paise - obligation.recovered_paise),
                )
            if credit:
                touch = session.scalar(
                    select(Action)
                    .where(
                        Action.case_id == case.id,
                        Action.executed_at.is_not(None),
                        Action.executed_at <= signal.occurred_at,
                    )
                    .order_by(Action.executed_at.desc(), Action.step_index.desc())
                    .limit(1)
                )
                settlement.credited_paise = credit
                settlement.credited_action_id = touch.id if touch else None
                settlement.organic = case.arm == Arm.HOLDOUT or touch is None
                obligation.recovered_paise += credit
                attribution = session.scalar(
                    select(RecoveryAttribution).where(RecoveryAttribution.case_id == case.id)
                )
                if attribution is None:
                    attribution = RecoveryAttribution(
                        case_id=case.id,
                        payment_entity_id=signal.payment_id,
                        amount_paise=0,
                        matched_by="provider_obligation",
                        credit_rule=get_measurement_config().attribution.credit_rule,
                        organic=settlement.organic,
                        paid_at=signal.occurred_at,
                    )
                    session.add(attribution)
                attribution.amount_paise += credit
                attribution.credited_action_id = settlement.credited_action_id
                attribution.credited_action_type = touch.action_type if touch else None
                attribution.touch_at = touch.executed_at if touch else None
                attribution.organic = attribution.organic and settlement.organic
                attribution.paid_at = signal.occurred_at
    if signal.amount_due_paise is not None:
        obligation.outstanding_paise = min(
            obligation.outstanding_paise
            if obligation.outstanding_paise is not None
            else signal.amount_due_paise,
            signal.amount_due_paise,
        )
    fully_paid = signal.settlement == "full_settlement" or signal.amount_due_paise == 0
    if fully_paid:
        obligation.settled_at = obligation.settled_at or signal.occurred_at
        obligation.outstanding_paise = 0
    if case and case.outcome != CaseOutcome.RECOVERED and (fully_paid or new_settlement):
        append_event(
            session,
            case,
            kind="VERIFYING" if fully_paid else "SETTLEMENT_OBSERVED",
            actor="provider_reconciler",
            payload={
                "amount_paise": signal.amount_paise,
                "amount_due_paise": signal.amount_due_paise,
                "credited_paise": settlement.credited_paise if settlement else 0,
                "settlement_pending_reconciliation": fully_paid and not signal.payment_id,
            },
        )
        if fully_paid:
            cancelled = _cancel_actions(session, case)
            case.outcome, case.closed_at = CaseOutcome.RECOVERED.value, signal.occurred_at
            append_event(
                session,
                case,
                kind="CLOSED",
                actor="attribution_verifier",
                payload={
                    "outcome": "RECOVERED",
                    "cancelled_action_ids": cancelled,
                    "attribution": {
                        "credited": obligation.recovered_paise > 0,
                        "matched_by": "provider_obligation",
                    },
                },
            )
    session.flush()
    return case


def _registered_obligation(
    session: Session, merchant_id: str, entity_id: str, root_id: str | None
) -> ProviderObligation | None:
    # Legacy demo routes are test-mode only. New surfaces pass ProviderScope explicitly.
    for identifier in (entity_id, root_id):
        if not identifier:
            continue
        row = session.scalar(
            select(ProviderEntity).where(
                ProviderEntity.merchant_id == merchant_id,
                ProviderEntity.provider == "razorpay",
                ProviderEntity.mode == "test",
                ProviderEntity.provider_entity_id == identifier,
            )
        )
        if row and row.obligation_id:
            obligation = session.get(ProviderObligation, row.obligation_id)
            while obligation.alias_of:
                obligation = session.get(ProviderObligation, obligation.alias_of)
            return obligation
    return None


def registered_risk(session: Session, signal):
    obligation = _registered_obligation(
        session, signal.merchant_id, signal.entity_id, signal.entity_root_id
    )
    if obligation is None:
        return None
    if obligation.entity_type != "order":
        return (None, False)
    risk = RiskSignal(
        scope=ProviderScope(merchant_id=signal.merchant_id),
        entity=EntityRef(entity_type=signal.entity_type, entity_id=signal.entity_id),
        root=EntityRef(entity_type="order", entity_id=signal.entity_root_id)
        if signal.entity_root_id
        else None,
        obligation=ObligationRef(
            entity_type=obligation.entity_type, entity_id=obligation.provider_entity_id
        ),
        source="browser_provider_reconciled"
        if signal.leak_type == LeakType.CHECKOUT_ABANDON
        else "razorpay_webhook",
        occurred_at=signal.occurred_at,
        leak_type=signal.leak_type,
        customer_id=signal.customer_id,
        amount_due_paise=signal.amount_at_risk,
        currency=signal.currency,
    )
    return record_risk(session, risk, legacy_signal=signal)


def registered_recovery(session: Session, signal) -> tuple[bool, RecoveryCase | None]:
    obligation = _registered_obligation(
        session, signal.merchant_id, signal.entity_id, signal.entity_root_id
    )
    if obligation is None:
        return False, None
    if obligation.entity_type != "order":
        return True, None  # legacy order success cannot settle an invoice or another cycle
    payment_id = signal.entity_id if signal.entity_id.startswith("pay_") else None
    recovery = RecoverySignal(
        scope=ProviderScope(merchant_id=signal.merchant_id),
        entity=EntityRef(
            entity_type="payment" if payment_id else "order", entity_id=signal.entity_id
        ),
        root=EntityRef(entity_type="order", entity_id=signal.entity_root_id)
        if signal.entity_root_id
        else None,
        obligation=ObligationRef(
            entity_type=obligation.entity_type, entity_id=obligation.provider_entity_id
        ),
        source="razorpay_webhook",
        occurred_at=signal.occurred_at,
        currency=signal.currency,
        payment_id=payment_id,
        amount_paise=signal.amount_paise if payment_id else 0,
        amount_due_paise=0,
        settlement="full_settlement",
    )
    return True, record_recovery(session, recovery)


def case_for_session(session: Session, demo: DemoSession) -> RecoveryCase | None:
    entity = find_entity(
        session,
        ProviderScope(merchant_id=demo.merchant_id, mode=demo.provider_mode),
        EntityRef(entity_type=demo.primary_entity_type, entity_id=demo.primary_entity_id),
    )
    if entity and entity.obligation_id:
        obligation = session.get(ProviderObligation, entity.obligation_id)
        while obligation.alias_of:
            obligation = session.get(ProviderObligation, obligation.alias_of)
        return session.get(RecoveryCase, obligation.case_id) if obligation.case_id else None
    if demo.primary_entity_type != "order":
        if demo.primary_entity_type == "subscription" and entity:
            invoice_id = entity.safe_metadata.get("affected_invoice_id")
            if invoice_id:
                cycle = find_entity(
                    session,
                    ProviderScope(merchant_id=demo.merchant_id, mode=demo.provider_mode),
                    EntityRef(entity_type="invoice", entity_id=invoice_id),
                )
                if cycle and cycle.obligation_id:
                    obligation = session.get(ProviderObligation, cycle.obligation_id)
                    while obligation.alias_of:
                        obligation = session.get(ProviderObligation, obligation.alias_of)
                    return (
                        session.get(RecoveryCase, obligation.case_id)
                        if obligation.case_id
                        else None
                    )
        return None  # subscription parents can have multiple cases; use only reconciled cycle
    from leakproof.demo.contracts import live_case_dedupe_key

    return session.scalar(
        select(RecoveryCase).where(
            RecoveryCase.merchant_id == demo.merchant_id,
            RecoveryCase.dedupe_key == live_case_dedupe_key(demo.id, demo.razorpay_order_id),
        )
    )


def order_recovery_supported(session: Session, demo: DemoSession) -> bool:
    if demo.primary_entity_type != "order" or demo.provider_mode != "test":
        return False
    obligation = _registered_obligation(session, demo.merchant_id, demo.primary_entity_id, None)
    return obligation is None or (
        obligation.entity_type == "order" and not obligation.reconciliation_required
    )


def needs_payment_reconciliation(session: Session, demo: DemoSession) -> bool:
    obligation = _registered_obligation(session, demo.merchant_id, demo.primary_entity_id, None)
    return bool(
        obligation
        and obligation.case_id
        and not obligation.reconciliation_required
        and obligation.recovered_paise < (obligation.detected_due_paise or 0)
    )
