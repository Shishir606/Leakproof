"""Track C subscription recovery with exact billing-cycle reconciliation.

Razorpay owns recurring retries. This module can create a test subscription,
observe it, and offer customer-authorized card replacement; it never charges,
resumes, or retries a subscription.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from leakproof.audit.timeline import append_event
from leakproof.config import Settings
from leakproof.demo.contracts import SubscriptionRecoveryBootstrap, SubscriptionSessionCreated
from leakproof.demo.invoices import safe_invoice_url
from leakproof.demo.security import (
    RecoveryTokenClaims,
    encrypt_recipient,
    issue_resource_recovery_token,
    issue_session_token,
    recipient_hash,
)
from leakproof.models.db import Customer, DemoSession, ProviderEntity, RecoveryCase
from leakproof.models.domain import Arm, LeakType
from leakproof.models.resources import (
    EntityRef,
    EntityStateSignal,
    ObligationRef,
    ProviderScope,
    RecoverySignal,
    RiskSignal,
)
from leakproof.provenance import DataProvenance
from leakproof.provider_resources import (
    _cancel_actions,
    _lock_scope,
    find_entity,
    get_obligation,
    record_recovery,
    record_risk,
    record_state,
    register_entity,
)
from leakproof.providers.contracts import (
    CreateSubscriptionRequest,
    Invoice,
    ProviderError,
    Subscription,
)
from leakproof.services import NormalizedSignal, ensure_merchant, new_id

TERMINAL_OR_INTENTIONAL = {"paused", "cancelled", "completed", "expired"}
RISK_STATES = {"pending", "halted"}


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def subscription_scope(demo: DemoSession) -> ProviderScope:
    return ProviderScope(merchant_id=demo.merchant_id, mode=demo.provider_mode)


def subscription_entity(session: Session, demo: DemoSession) -> ProviderEntity | None:
    return find_entity(
        session,
        subscription_scope(demo),
        EntityRef(entity_type="subscription", entity_id=demo.primary_entity_id),
    )


def _provider_error(
    operation: str, error_class: str, message: str, *, retryable: bool = False
) -> ProviderError:
    return ProviderError("razorpay", operation, error_class, retryable, message)


def _validate_subscription(demo: DemoSession, entity: ProviderEntity, item: Subscription) -> None:
    if item.id != demo.primary_entity_id or item.plan_id != entity.safe_metadata.get("plan_id"):
        raise _provider_error(
            "reconcile_subscription",
            "response_mismatch",
            "Subscription identity or plan did not match",
        )


def _validate_invoice(subscription_id: str, item: Invoice) -> None:
    if item.subscription_id != subscription_id or item.amount_paise <= 0:
        raise _provider_error(
            "reconcile_subscription",
            "response_mismatch",
            "Subscription invoice relationship did not match",
        )


def create_subscription_session(
    session: Session, request, *, provider, settings: Settings, now: datetime
) -> SubscriptionSessionCreated:
    from leakproof.demo.service import _record_provider_call, _signing_secret

    plan_id = settings.demo_subscription_plan_id
    if not plan_id and settings.mode == "simulation":
        plan_id = "plan_simulated"
    if not plan_id:
        raise _provider_error(
            "create_subscription",
            "subscription_setup_required",
            "Configure one reusable Razorpay test plan",
        )
    session_id, customer_id = new_id("demo"), new_id("demo_customer")
    try:
        item = provider.create_subscription(
            CreateSubscriptionRequest(
                plan_id=plan_id,
                total_count=settings.demo_subscription_total_count,
                notes={"leakproof_session_id": session_id},
            )
        )
        if (
            item.plan_id != plan_id
            or item.status != "created"
            or not safe_invoice_url(item.short_url)
        ):
            raise _provider_error(
                "create_subscription",
                "response_mismatch",
                "Subscription setup response did not match",
            )
    except ProviderError as exc:
        _record_provider_call(
            session,
            session_id=None,
            provider="razorpay",
            operation="create_subscription",
            request_id=exc.request_id,
            latency_ms=exc.latency_ms,
            status="failed",
            metadata={},
            error_class=exc.error_class,
        )
        session.commit()
        raise

    ensure_merchant(session, settings.default_merchant_id)
    session.add(Customer(id=customer_id, merchant_id=settings.default_merchant_id))
    allowlisted = (
        request.recipient is not None and request.recipient in settings.allowed_demo_emails
    )
    secret = _signing_secret(settings)
    demo = DemoSession(
        id=session_id,
        merchant_id=settings.default_merchant_id,
        customer_id=customer_id,
        scenario_type="SUBSCRIPTION_HALT",
        primary_entity_type="subscription",
        primary_entity_id=item.id,
        provider_mode="test",
        setup_state="READY",
        amount_paise=0,
        currency=settings.demo_currency,
        state="CREATED",
        capability_evidence=DataProvenance.CONTRACT_VERIFIED
        if settings.mode == "live_demo"
        else DataProvenance.SIMULATED_END_TO_END,
        recipient_ciphertext=encrypt_recipient(request.recipient, secret) if allowlisted else None,
        recipient_hash=recipient_hash(request.recipient, secret) if request.recipient else None,
        expires_at=now + timedelta(minutes=settings.demo_session_ttl_minutes),
        created_at=now,
        updated_at=now,
    )
    session.add(demo)
    session.flush()
    entity = register_entity(
        session,
        subscription_scope(demo),
        EntityRef(entity_type="subscription", entity_id=item.id),
        session_id=demo.id,
        role="primary",
    )
    entity.status = item.status
    entity.state_observed_at = now
    entity.safe_metadata = {
        "plan_id": plan_id,
        "payment_method": item.payment_method,
        "paid_count": item.paid_count,
        "remaining_count": item.remaining_count,
        "retry_count": 0,
        "authorization_url_safe": True,
    }
    _record_provider_call(
        session,
        session_id=demo.id,
        provider="razorpay",
        operation="create_subscription",
        request_id=item.request_id,
        latency_ms=0,
        status="succeeded",
        metadata={
            "status": item.status,
            "provider_notifications_disabled": True,
            "reusable_plan": True,
        },
    )
    session.commit()
    return SubscriptionSessionCreated(
        scenario_type="SUBSCRIPTION_HALT",
        session_id=demo.id,
        primary_entity_id=item.id,
        session_token=issue_session_token(demo.id, demo.merchant_id, demo.expires_at, secret),
        setup_state=demo.setup_state,
        amount_paise=0,
        currency=demo.currency,
        expires_at=demo.expires_at,
        email_mode="allowlisted" if allowlisted else "preview_only",
        authorization_url=item.short_url,
    )


def _payment_snapshot(provider, invoice: Invoice, now: datetime):
    payments = provider.list_order_payments(invoice.order_id) if invoice.order_id else []
    seen = {}
    for payment in payments:
        if (
            payment.order_id != invoice.order_id
            or payment.invoice_id not in {None, invoice.id}
            or payment.currency != invoice.currency
            or payment.amount_paise <= 0
            or payment.amount_paise > invoice.amount_paise
            or type(payment.created_at) is not int
            or payment.created_at < 0
            or payment.created_at > now.timestamp()
            or (payment.id in seen and payment != seen[payment.id])
        ):
            raise _provider_error(
                "reconcile_subscription",
                "response_mismatch",
                "Subscription payment relationship did not match",
            )
        seen[payment.id] = payment
    captures = [p for p in seen.values() if p.status == "captured"]
    if sum(p.amount_paise for p in captures) != invoice.amount_paid_paise:
        raise _provider_error(
            "reconcile_subscription",
            "invoice_reconciliation_pending",
            "Invoice and captured payments require reconciliation",
            retryable=True,
        )
    return captures


def reconcile_subscription(
    session: Session,
    session_id: str,
    *,
    provider,
    settings: Settings,
    now: datetime | None = None,
    source: str = "razorpay_api",
    operation: str = "reconcile_subscription",
    explicit_invoice_id: str | None = None,
    commit: bool = True,
) -> RecoveryCase | None:
    from leakproof.demo.service import _record_provider_call

    now = utc(now or datetime.now(UTC))
    demo = session.get(DemoSession, session_id)
    if (
        not demo
        or demo.primary_entity_type != "subscription"
        or demo.merchant_id != settings.default_merchant_id
        or demo.provider_mode != "test"
    ):
        raise ValueError("subscription session scope mismatch")
    _lock_scope(session, subscription_scope(demo))
    session.refresh(demo)
    entity = subscription_entity(session, demo)
    try:
        if not entity or not entity.safe_metadata.get("plan_id"):
            raise _provider_error(
                operation, "subscription_setup_required", "Subscription setup policy is missing"
            )
        item = provider.fetch_subscription(demo.primary_entity_id)
        _validate_subscription(demo, entity, item)
        invoices = provider.list_subscription_invoices(item.id)
        for invoice in invoices:
            _validate_invoice(item.id, invoice)
        by_id = {invoice.id: invoice for invoice in invoices}
        if len(by_id) != len(invoices) or (
            explicit_invoice_id and explicit_invoice_id not in by_id
        ):
            raise _provider_error(
                operation, "response_mismatch", "Webhook cycle did not belong to the subscription"
            )

        prior = entity.safe_metadata
        current_id = prior.get("affected_invoice_id")
        unpaid = [
            i
            for i in invoices
            if i.amount_due_paise > 0
            and i.status in {"issued", "partially_paid", "expired", "cancelled"}
        ]
        current = by_id.get(current_id)
        # Keep an older unpaid cycle canonical even when a future-cycle event arrives.
        # Once it is paid, an explicit or sole new unpaid cycle may become canonical.
        affected = current if current and current.amount_due_paise > 0 else None
        if affected is None and explicit_invoice_id:
            affected = by_id.get(explicit_invoice_id)
        if affected is None and len(unpaid) == 1:
            affected = unpaid[0]
        if affected is None and current is not None and not unpaid:
            affected = current  # retain the just-settled cycle long enough to close it
        ambiguous = affected is None and len(unpaid) > 1

        entity.status = item.status
        entity.state_observed_at = now
        retry_count = int(prior.get("retry_count", 0))
        if item.status in RISK_STATES and (
            prior.get("observed_status") != item.status or explicit_invoice_id
        ):
            retry_count += 1
        entity.safe_metadata = {
            **prior,
            "payment_method": item.payment_method,
            "paid_count": item.paid_count,
            "remaining_count": item.remaining_count,
            "current_start": item.current_start,
            "current_end": item.current_end,
            "charge_at": item.charge_at,
            "retry_count": retry_count,
            "observed_status": item.status,
            "method_allowlisted": item.payment_method in settings.allowed_subscription_methods,
            "affected_invoice_id": affected.id if affected else current_id,
            "ambiguous_unpaid_cycles": ambiguous,
            "check_failed": False,
        }
        state_name = (
            "active"
            if item.status in {"active", "authenticated"}
            else "cancelled"
            if item.status in TERMINAL_OR_INTENTIONAL
            else item.status
        )
        if state_name in {"pending", "halted", "active", "cancelled"}:
            record_state(
                session,
                EntityStateSignal(
                    scope=subscription_scope(demo),
                    entity=EntityRef(entity_type="subscription", entity_id=item.id),
                    source=source,
                    occurred_at=now,
                    state=state_name,
                ),
            )
            entity.status = item.status  # preserve distinct intentional/terminal provider state

        case = None
        if affected:
            captures = _payment_snapshot(provider, affected, now)
            ref = ObligationRef(entity_type="invoice", entity_id=affected.id)
            obligation = get_obligation(session, subscription_scope(demo), ref, affected.currency)
            invoice_entity = register_entity(
                session,
                subscription_scope(demo),
                ref,
                session_id=demo.id,
                root=EntityRef(entity_type="subscription", entity_id=item.id),
                obligation=obligation,
                role="billing_cycle",
            )
            invoice_entity.status = affected.status
            invoice_entity.state_observed_at = now
            invoice_entity.safe_metadata = {
                "amount_due_paise": affected.amount_due_paise,
                "amount_paid_paise": affected.amount_paid_paise,
                "order_id": affected.order_id,
            }
            if affected.order_id:
                register_entity(
                    session,
                    subscription_scope(demo),
                    EntityRef(entity_type="order", entity_id=affected.order_id),
                    session_id=demo.id,
                    root=ref,
                    obligation=obligation,
                )
            demo.amount_paise = affected.amount_paise
            demo.currency = affected.currency
            if item.status in RISK_STATES and affected.amount_due_paise > 0:
                case, _ = record_risk(
                    session,
                    RiskSignal(
                        scope=subscription_scope(demo),
                        entity=EntityRef(entity_type="subscription", entity_id=item.id),
                        root=ref,
                        obligation=ref,
                        source=source,
                        occurred_at=now,
                        leak_type=LeakType.SUBSCRIPTION_HALT,
                        customer_id=demo.customer_id,
                        amount_due_paise=affected.amount_due_paise,
                        baseline_paid_paise=affected.amount_paid_paise,
                        currency=affected.currency,
                    ),
                    session_id=demo.id,
                    legacy_signal=NormalizedSignal(
                        merchant_id=demo.merchant_id,
                        customer_id=demo.customer_id,
                        leak_type=LeakType.SUBSCRIPTION_HALT,
                        entity_type="subscription",
                        entity_id=item.id,
                        entity_root_id=affected.id,
                        amount_at_risk=affected.amount_due_paise,
                        currency=affected.currency,
                        evidence={"source": source, "subscription_state": item.status},
                        occurred_at=now,
                        dedupe_key_override=obligation.id,
                        arm_override=Arm.TREATMENT,
                    ),
                )
                demo.state = "AT_RISK"
            else:
                case = session.get(RecoveryCase, obligation.case_id) if obligation.case_id else None
            for payment in captures:
                record_recovery(
                    session,
                    RecoverySignal(
                        scope=subscription_scope(demo),
                        entity=EntityRef(entity_type="payment", entity_id=payment.id),
                        root=ref,
                        obligation=ref,
                        source=source,
                        occurred_at=now,
                        payment_id=payment.id,
                        amount_paise=payment.amount_paise,
                        amount_due_paise=affected.amount_due_paise,
                        currency=affected.currency,
                        settlement="captured_payment",
                    ),
                )
            obligation.outstanding_paise = affected.amount_due_paise
            if affected.status == "paid" and affected.amount_due_paise == 0 and captures:
                record_recovery(
                    session,
                    RecoverySignal(
                        scope=subscription_scope(demo),
                        entity=ref,
                        root=EntityRef(entity_type="subscription", entity_id=item.id),
                        obligation=ref,
                        source=source,
                        occurred_at=now,
                        amount_due_paise=0,
                        currency=affected.currency,
                        settlement="full_settlement",
                    ),
                )
                if current_id in {None, affected.id} and demo.state != "EXPIRED":
                    demo.state = "RECOVERED"
            if case and (
                prior.get("observed_status") != item.status
                or prior.get("cycle_status") != affected.status
                or prior.get("cycle_due") != affected.amount_due_paise
            ):
                append_event(
                    session,
                    case,
                    kind="SUBSCRIPTION_RECONCILED",
                    actor="razorpay_reconciler",
                    payload={
                        "subscription_status": item.status,
                        "cycle_status": affected.status,
                        "amount_due_paise": affected.amount_due_paise,
                        "retry_count": retry_count,
                        "service_recovered": item.status == "active",
                        "invoice_recovered": affected.status == "paid"
                        and affected.amount_due_paise == 0,
                    },
                )
            entity.safe_metadata = {
                **entity.safe_metadata,
                "cycle_status": affected.status,
                "cycle_due": affected.amount_due_paise,
            }
            if item.status in TERMINAL_OR_INTENTIONAL and case:
                _cancel_actions(session, case)
            customer = session.get(Customer, demo.customer_id)
            if customer and customer.dnc and case:
                _cancel_actions(session, case)
        if ambiguous:
            demo.setup_state = "ACTION_REQUIRED"
        elif item.status in TERMINAL_OR_INTENTIONAL:
            demo.setup_state = "ACTION_REQUIRED"
        else:
            demo.setup_state = "READY"
        demo.updated_at = now
        if settings.mode == "live_demo":
            demo.capability_evidence = DataProvenance.LIVE_PROVIDER_VERIFIED
        _record_provider_call(
            session,
            session_id=demo.id,
            provider="razorpay",
            operation=operation,
            request_id=item.request_id,
            latency_ms=0,
            status="succeeded",
            metadata={
                "subscription_status": item.status,
                "cycle_resolved": affected is not None,
                "unpaid_cycle_count": len(unpaid),
                "retry_owner": "razorpay",
            },
        )
        if commit:
            session.commit()
        return case
    except ProviderError as exc:
        session.rollback()
        demo = session.get(DemoSession, session_id)
        entity = subscription_entity(session, demo)
        if entity:
            entity.safe_metadata = {**entity.safe_metadata, "check_failed": True}
            entity.state_observed_at = now
        _record_provider_call(
            session,
            session_id=demo.id,
            provider="razorpay",
            operation=operation,
            request_id=exc.request_id,
            latency_ms=exc.latency_ms,
            status="failed",
            metadata={},
            error_class=exc.error_class,
        )
        session.commit()
        raise


def subscription_view(session: Session, demo: DemoSession, now: datetime) -> dict | None:
    if demo.primary_entity_type != "subscription":
        return None
    entity = subscription_entity(session, demo)
    if not entity:
        return None
    data = entity.safe_metadata
    invoice_id = data.get("affected_invoice_id")
    obligation = None
    if invoice_id:
        obligation = get_obligation(
            session,
            subscription_scope(demo),
            ObligationRef(entity_type="invoice", entity_id=invoice_id),
            demo.currency,
        )
    due = (
        obligation.outstanding_paise
        if obligation and obligation.outstanding_paise is not None
        else int(data.get("cycle_due", 0))
    )
    customer = session.get(Customer, demo.customer_id)
    method_available = bool(
        invoice_id
        and due > 0
        and entity.status in RISK_STATES
        and data.get("method_allowlisted")
        and not (customer and customer.dnc)
    )
    if data.get("check_failed") or data.get("ambiguous_unpaid_cycles"):
        disposition = "provider_retry" if data.get("check_failed") else "merchant_review"
    elif entity.status == "created":
        disposition = "authorization_required"
    elif due == 0 and invoice_id:
        disposition = "paid"
    elif entity.status == "active" and due > 0:
        disposition = "active_with_arrears"
    elif method_available:
        disposition = "method_update"
    else:
        disposition = "merchant_review"
    return {
        "provider_status": entity.status or "unknown",
        "payment_method": data.get("payment_method"),
        "cycle_resolved": bool(invoice_id),
        "cycle_status": data.get("cycle_status"),
        "detected_balance_paise": obligation.detected_due_paise if obligation else None,
        "outstanding_balance_paise": due,
        "recovered_paise": obligation.recovered_paise if obligation else 0,
        "retry_owner": "razorpay",
        "retry_count": int(data.get("retry_count", 0)),
        "method_update_available": method_available,
        "authorization_repaired": bool(data.get("authorization_repaired")),
        "disposition": disposition,
        "last_checked_at": entity.state_observed_at,
    }


def subscription_recovery_token(demo: DemoSession, *, settings: Settings, now: datetime) -> str:
    from leakproof.demo.service import _signing_secret

    return issue_resource_recovery_token(
        RecoveryTokenClaims(
            version=2,
            session_id=demo.id,
            merchant_id=demo.merchant_id,
            scenario_type="SUBSCRIPTION_HALT",
            entity=EntityRef(entity_type="subscription", entity_id=demo.primary_entity_id),
            purpose="subscription_method_update",
            amount_paise=demo.amount_paise,
            currency=demo.currency,
            expires_at=min(utc(demo.expires_at), now + timedelta(minutes=30)),
        ),
        _signing_secret(settings),
    )


def subscription_bootstrap(
    session: Session, claims, *, provider, settings: Settings, now: datetime
):
    from leakproof.demo.service import RecoveryExpired, RecoveryTokenInvalid

    demo = session.get(DemoSession, claims.session_id)
    if (
        not demo
        or claims.version != 2
        or claims.purpose != "subscription_method_update"
        or demo.primary_entity_type != "subscription"
        or demo.primary_entity_id != claims.entity.entity_id
        or demo.merchant_id != claims.merchant_id
        or demo.scenario_type != claims.scenario_type
        or demo.amount_paise != claims.amount_paise
        or demo.currency != claims.currency
    ):
        raise RecoveryTokenInvalid("invalid recovery token")
    if utc(demo.expires_at) <= now or demo.state == "EXPIRED":
        raise RecoveryExpired("demo session has expired")
    reconcile_subscription(
        session,
        demo.id,
        provider=provider,
        settings=settings,
        now=now,
        operation="recovery_subscription_check",
    )
    view = subscription_view(session, demo, now)
    if not view or not view["method_update_available"]:
        raise _provider_error(
            "recovery_subscription_check",
            "subscription_not_recoverable",
            "Subscription method update is unavailable",
        )
    return SubscriptionRecoveryBootstrap(
        session_id=demo.id,
        razorpay_key_id=settings.razorpay_key_id or "rzp_test_simulated",
        subscription_id=demo.primary_entity_id,
        expires_at=min(claims.expires_at, utc(demo.expires_at)),
    )


def process_subscription_webhook(
    session: Session, event, *, provider, settings: Settings, now: datetime | None = None
) -> tuple[bool, str | None]:
    payload = event.payload.get("payload", {})
    sub = payload.get("subscription", {}).get("entity", {})
    payment = payload.get("payment", {}).get("entity", {})
    invoice = payload.get("invoice", {}).get("entity", {})
    subscription_id = sub.get("id") or invoice.get("subscription_id")
    invoice_id = invoice.get("id") or payment.get("invoice_id")
    if not subscription_id and invoice_id:
        linked = session.scalar(
            select(ProviderEntity).where(
                ProviderEntity.merchant_id == event.merchant_id,
                ProviderEntity.provider == "razorpay",
                ProviderEntity.mode == "test",
                ProviderEntity.entity_type == "invoice",
                ProviderEntity.provider_entity_id == invoice_id,
                ProviderEntity.root_entity_type == "subscription",
            )
        )
        subscription_id = linked.root_entity_id if linked else None
    if not subscription_id:
        # Invoice-owned sessions also receive subscription.charged wakeups.
        # Let the invoice reconciler consume an explicit invoice relationship.
        return event.event_type.startswith("subscription.") and not invoice_id, None
    demo = session.scalar(
        select(DemoSession).where(
            DemoSession.merchant_id == event.merchant_id,
            DemoSession.provider_mode == "test",
            DemoSession.primary_entity_type == "subscription",
            DemoSession.primary_entity_id == subscription_id,
        )
    )
    if not demo:
        return True, None
    case = reconcile_subscription(
        session,
        demo.id,
        provider=provider,
        settings=settings,
        now=now,
        source="razorpay_webhook",
        explicit_invoice_id=invoice_id,
    )
    return True, case.id if case else None


def reconcile_subscription_sessions(
    *, session_factory, provider, settings: Settings, now: datetime | None = None, limit: int = 100
) -> dict:
    now = utc(now or datetime.now(UTC))
    with session_factory() as session:
        ids = list(
            session.scalars(
                select(DemoSession.id)
                .join(ProviderEntity, ProviderEntity.session_id == DemoSession.id)
                .where(
                    DemoSession.primary_entity_type == "subscription",
                    ProviderEntity.entity_type == "subscription",
                    (ProviderEntity.state_observed_at.is_(None))
                    | (
                        ProviderEntity.state_observed_at
                        <= now - timedelta(seconds=settings.subscription_reconcile_seconds)
                    ),
                )
                .order_by(ProviderEntity.state_observed_at.asc().nullsfirst())
            )
        )[:limit]
    cases, failed = [], 0
    for session_id in ids:
        with session_factory() as session:
            try:
                case = reconcile_subscription(
                    session, session_id, provider=provider, settings=settings, now=now
                )
                if case:
                    from leakproof.demo.email import schedule_demo_recovery_email
                    from leakproof.diagnosis import diagnose_case

                    cases.append(case.id)
                    diagnose_case(session, case.id)
                    view = subscription_view(session, session.get(DemoSession, session_id), now)
                    if view["method_update_available"] and case.outcome != "RECOVERED":
                        schedule_demo_recovery_email(session, case.id, settings=settings, now=now)
                    session.commit()
            except ProviderError:
                failed += 1
    return {
        "sensor": "subscription_health",
        "scanned": len(ids),
        "signals": len(set(cases)),
        "failed": failed,
    }
