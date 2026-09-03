"""Track B: app-owned aging and read-only recovery of the original invoice.

Provider expiry is never a due date. Webhooks are wakeups: current, merchant-scoped
API reads supply balances and payment identities, so delivery ordering cannot pay twice.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from leakproof.audit.timeline import append_event
from leakproof.config import Settings
from leakproof.demo.contracts import InvoiceSessionCreated
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
    LEAK_PRECEDENCE,
    EntityRef,
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
    register_entity,
)
from leakproof.providers.contracts import CreateInvoiceRequest, Invoice, ProviderError
from leakproof.services import NormalizedSignal, ensure_merchant, new_id


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def invoice_scope(demo: DemoSession) -> ProviderScope:
    return ProviderScope(merchant_id=demo.merchant_id, mode=demo.provider_mode)


def invoice_entity(session: Session, demo: DemoSession) -> ProviderEntity | None:
    return find_entity(
        session,
        invoice_scope(demo),
        EntityRef(
            entity_type="invoice",
            entity_id=demo.primary_entity_id,
        ),
    )


def safe_invoice_url(url: str | None) -> bool:
    # Razorpay currently returns /rzp/... while its invoice API examples use /i/....
    # Accept both hosted-link forms with an exact origin and no URL decorations.
    if not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return bool(
        parts.scheme == "https"
        and parts.netloc == "rzp.io"
        and re.fullmatch(r"/(?:i|rzp)/[A-Za-z0-9_-]+", parts.path)
        and not parts.query
        and not parts.fragment
    )


def disposition(
    status: str, due: int, expiry: int | None, now: datetime, *, url_safe: bool = True
) -> str:
    if status == "paid" and due == 0:
        return "paid"
    if status in {"issued", "partially_paid"} and due > 0:
        if expiry is not None and expiry <= now.timestamp():
            return "merchant_review"
        return "payable" if url_safe else "merchant_review"
    return "merchant_review"


def invoice_view(session: Session, demo: DemoSession, now: datetime) -> dict | None:
    if demo.primary_entity_type != "invoice":
        return None
    entity = invoice_entity(session, demo)
    if entity is None:
        return None
    data = entity.safe_metadata
    if "business_due_at" not in data:
        return None  # Foundation-only records have no validated invoice policy.
    due_at = datetime.fromisoformat(data["business_due_at"])
    obligation = get_obligation(
        session,
        invoice_scope(demo),
        ObligationRef(entity_type="invoice", entity_id=demo.primary_entity_id),
        demo.currency,
    )
    due = obligation.outstanding_paise
    if due is None:
        due = data.get("amount_due_paise", demo.amount_paise)
    decision = disposition(
        entity.status or "unknown",
        due,
        data.get("expire_by"),
        now,
        url_safe=data.get("url_safe", False),
    )
    if data.get("check_failed") or obligation.reconciliation_required:
        decision = "provider_retry"
    elapsed = max(0, int((now - due_at).total_seconds()))
    return {
        "provider_status": entity.status or "unknown",
        "business_due_at": due_at,
        "business_overdue": now >= due_at and due > 0,
        "aging_bucket": "not_due"
        if now < due_at
        else "under_1_day"
        if elapsed < 86400
        else "1_to_7_days"
        if elapsed < 604800
        else "over_7_days",
        "provider_expires_at": datetime.fromtimestamp(data["expire_by"], UTC)
        if data.get("expire_by") is not None
        else None,
        "detected_balance_paise": obligation.detected_due_paise,
        "outstanding_balance_paise": due,
        "amount_paid_paise": data.get("amount_paid_paise", 0),
        "recovered_paise": obligation.recovered_paise,
        "disposition": decision,
        "last_checked_at": entity.state_observed_at,
        "partial_payment": data.get("partial_payment", False),
    }


def create_invoice_session(
    session: Session, request, *, provider, settings: Settings, now: datetime
) -> InvoiceSessionCreated:
    from leakproof.demo.service import _record_provider_call, _signing_secret

    customer = settings.demo_invoice_customer_id
    if not customer:
        if settings.mode == "simulation":
            customer = "cust_simulated"
        else:
            raise ProviderError(
                "razorpay",
                "create_invoice",
                "invoice_setup_required",
                False,
                "Configure a test invoice customer before setup",
            )
    session_id, customer_id = new_id("demo"), new_id("demo_customer")
    try:
        invoice = provider.create_invoice(
            CreateInvoiceRequest(
                customer_id=customer,
                amount_paise=settings.demo_amount_paise,
                currency=settings.demo_currency,
                receipt=f"demo-{session_id}"[:40],
                line_item_name="Test invoice recovery",
                idempotency_key=f"invoice:{session_id}",
                expire_by=int(
                    (now + timedelta(minutes=settings.demo_invoice_expiry_minutes)).timestamp()
                ),
            )
        )
        if (
            invoice.amount_paise != settings.demo_amount_paise
            or invoice.currency != settings.demo_currency
            or invoice.customer_id != customer
            or invoice.status != "draft"
        ):
            raise ProviderError(
                "razorpay",
                "create_invoice",
                "response_mismatch",
                False,
                "Invoice setup response did not match",
            )
    except ProviderError as exc:
        _record_provider_call(
            session,
            session_id=None,
            provider="razorpay",
            operation="create_invoice",
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
        scenario_type="INVOICE_OVERDUE",
        primary_entity_type="invoice",
        primary_entity_id=invoice.id,
        provider_mode="test",
        setup_state="CREATING",
        amount_paise=invoice.amount_paise,
        currency=invoice.currency,
        state="CREATED",
        created_at=now,
        updated_at=now,
        capability_evidence=DataProvenance.ARCHITECTURE_READY
        if settings.mode == "live_demo"
        else DataProvenance.SIMULATED_END_TO_END,
        expires_at=now + timedelta(minutes=settings.demo_session_ttl_minutes),
        recipient_ciphertext=encrypt_recipient(request.recipient, secret) if allowlisted else None,
        recipient_hash=recipient_hash(request.recipient, secret) if request.recipient else None,
    )
    session.add(demo)
    session.flush()
    obligation = get_obligation(
        session,
        invoice_scope(demo),
        ObligationRef(entity_type="invoice", entity_id=invoice.id),
        invoice.currency,
    )
    entity = register_entity(
        session,
        invoice_scope(demo),
        EntityRef(entity_type="invoice", entity_id=invoice.id),
        session_id=demo.id,
        obligation=obligation,
        role="primary",
    )
    entity.status = invoice.status
    entity.safe_metadata = {
        "business_due_at": (now + timedelta(seconds=settings.demo_invoice_due_seconds)).isoformat(),
        "aging_policy": "setup_time_plus_seconds_v1",
        "aging_seconds": settings.demo_invoice_due_seconds,
        "provider_customer_id": customer,
        "amount_due_paise": invoice.amount_due_paise,
        "amount_paid_paise": invoice.amount_paid_paise,
        "partial_payment": invoice.partial_payment,
        "expire_by": invoice.expire_by,
    }
    _record_provider_call(
        session,
        session_id=demo.id,
        provider="razorpay",
        operation="create_invoice",
        request_id=invoice.request_id,
        latency_ms=0,
        status="succeeded",
        metadata={"provider_notifications_disabled": True, "status": "draft"},
    )
    session.commit()  # Retain the original draft if issuance times out.
    try:
        issued = provider.issue_invoice(invoice.id)
        validate_invoice(demo, entity, issued)
        if issued.status != "issued" or not issued.partial_payment:
            raise ProviderError(
                "razorpay",
                "issue_invoice",
                "unexpected_invoice_state",
                False,
                "Invoice issuance requires merchant review",
            )
        _record_provider_call(
            session,
            session_id=demo.id,
            provider="razorpay",
            operation="issue_invoice",
            request_id=issued.request_id,
            latency_ms=0,
            status="succeeded",
            metadata={"status": issued.status},
        )
        entity.safe_metadata = {
            **entity.safe_metadata,
            "order_id": issued.order_id,
            "expire_by": issued.expire_by,
            "url_safe": safe_invoice_url(issued.short_url),
        }
        entity.status = issued.status
        if issued.order_id:
            register_entity(
                session,
                invoice_scope(demo),
                EntityRef(entity_type="order", entity_id=issued.order_id),
                root=EntityRef(entity_type="invoice", entity_id=invoice.id),
                obligation=obligation,
                session_id=demo.id,
            )
        demo.setup_state = "READY"
    except ProviderError as exc:
        demo.setup_state = "ACTION_REQUIRED"
        _record_provider_call(
            session,
            session_id=demo.id,
            provider="razorpay",
            operation="issue_invoice",
            request_id=exc.request_id,
            latency_ms=exc.latency_ms,
            status="failed",
            metadata={},
            error_class=exc.error_class,
        )
    session.commit()
    return InvoiceSessionCreated(
        session_id=demo.id,
        primary_entity_id=invoice.id,
        setup_state=demo.setup_state,
        session_token=issue_session_token(demo.id, demo.merchant_id, demo.expires_at, secret),
        amount_paise=demo.amount_paise,
        currency=demo.currency,
        expires_at=demo.expires_at,
        email_mode="allowlisted" if allowlisted else "preview_only",
    )


def validate_invoice(demo: DemoSession, entity: ProviderEntity, invoice: Invoice) -> None:
    if (
        invoice.id != demo.primary_entity_id
        or invoice.amount_paise != demo.amount_paise
        or invoice.currency != demo.currency
        or invoice.customer_id != entity.safe_metadata["provider_customer_id"]
        or (
            entity.safe_metadata.get("order_id")
            and invoice.order_id != entity.safe_metadata["order_id"]
        )
    ):
        raise ProviderError(
            "razorpay",
            "reconcile_invoice",
            "response_mismatch",
            False,
            "Invoice ownership or balance identity did not match",
        )


def reconcile_invoice(
    session: Session,
    session_id: str,
    *,
    provider,
    settings: Settings,
    now: datetime | None = None,
    source: str = "razorpay_api",
    operation: str = "reconcile_invoice",
    commit: bool = True,
) -> RecoveryCase | None:
    from leakproof.demo.service import _record_provider_call

    now = utc(now or datetime.now(UTC))
    demo = session.get(DemoSession, session_id)
    if (
        demo is None
        or demo.primary_entity_type != "invoice"
        or demo.merchant_id != settings.default_merchant_id
        or demo.provider_mode != "test"
    ):
        raise ValueError("invoice session scope mismatch")
    _lock_scope(session, invoice_scope(demo))
    session.refresh(demo)
    entity = invoice_entity(session, demo)
    try:
        if entity is None or not entity.safe_metadata.get("business_due_at"):
            raise ProviderError(
                "razorpay",
                operation,
                "invoice_setup_required",
                False,
                "Invoice setup policy is missing",
            )
        invoice = provider.fetch_invoice(demo.primary_entity_id)
        validate_invoice(demo, entity, invoice)
        payments = provider.list_order_payments(invoice.order_id) if invoice.order_id else []
        seen = {}
        for payment in payments:
            if (
                not re.fullmatch(r"pay_[A-Za-z0-9_]+", payment.id)
                or payment.order_id != invoice.order_id
                or payment.currency != invoice.currency
                or payment.amount_paise <= 0
                or payment.amount_paise > invoice.amount_paise
                or payment.invoice_id not in {None, invoice.id}
                or (payment.id in seen and payment != seen[payment.id])
            ):
                raise ProviderError(
                    "razorpay",
                    operation,
                    "response_mismatch",
                    False,
                    "Invoice payment relationship did not match",
                )
            seen[payment.id] = payment
        captures = [p for p in seen.values() if p.status == "captured"]
        captured_total = sum(p.amount_paise for p in captures)
        if (
            captured_total != invoice.amount_paid_paise
            or any(
                type(p.created_at) is not int or p.created_at > now.timestamp() or p.created_at < 0
                for p in captures
            )
            or (invoice.status in {"issued", "partially_paid"} and not invoice.amount_due_paise)
            or (invoice.amount_paid_paise < entity.safe_metadata.get("amount_paid_paise", 0))
        ):
            raise ProviderError(
                "razorpay",
                operation,
                "invoice_reconciliation_pending",
                True,
                "Invoice and captured payments require reconciliation",
            )
        ref = ObligationRef(entity_type="invoice", entity_id=invoice.id)
        obligation = get_obligation(session, invoice_scope(demo), ref, invoice.currency)
        if obligation.settled_at and invoice.status != "paid":
            raise ProviderError(
                "razorpay",
                operation,
                "invoice_reconciliation_pending",
                True,
                "Settled invoice has a conflicting provider snapshot",
            )
        if invoice.order_id:
            register_entity(
                session,
                invoice_scope(demo),
                EntityRef(entity_type="order", entity_id=invoice.order_id),
                root=ref,
                obligation=obligation,
                session_id=demo.id,
            )
        if obligation.reconciliation_required:
            # Preserve the foundation's quarantine and cancelled contacts before logging failure.
            session.commit()
            raise ProviderError(
                "razorpay",
                operation,
                "relationship_reconciliation_required",
                False,
                "Invoice relationship requires merchant review",
            )
        if invoice.subscription_id:
            register_entity(
                session,
                invoice_scope(demo),
                EntityRef(entity_type="subscription", entity_id=invoice.subscription_id),
            )
        previous = entity.safe_metadata
        decision = disposition(
            invoice.status,
            invoice.amount_due_paise,
            invoice.expire_by,
            now,
            url_safe=safe_invoice_url(invoice.short_url),
        )
        entity.status, entity.state_observed_at = invoice.status, now
        entity.safe_metadata = {
            **previous,
            "order_id": invoice.order_id,
            "amount_due_paise": invoice.amount_due_paise,
            "amount_paid_paise": invoice.amount_paid_paise,
            "expire_by": invoice.expire_by,
            "partial_payment": invoice.partial_payment,
            "url_safe": safe_invoice_url(invoice.short_url),
            "check_failed": False,
        }
        due_at = datetime.fromisoformat(previous["business_due_at"])

        # Capture pre-detection payments first: these cannot later earn recovery credit.
        def record_payment(payment):
            return record_recovery(
                session,
                RecoverySignal(
                    scope=invoice_scope(demo),
                    entity=EntityRef(entity_type="payment", entity_id=payment.id),
                    root=ref,
                    obligation=ref,
                    source=source,
                    occurred_at=now,  # Verified capture observation; creation may predate capture.
                    payment_id=payment.id,
                    amount_paise=payment.amount_paise,
                    currency=invoice.currency,
                    settlement="captured_payment",
                ),
            )

        if obligation.case_id is None:
            for payment in captures:
                record_payment(payment)
        risk = (
            invoice.amount_due_paise > 0
            and invoice.amount_paise > 0
            and (
                invoice.status in {"expired", "cancelled", "deleted"}
                or invoice.status in {"issued", "partially_paid"}
                and now >= due_at
            )
        )
        case = session.get(RecoveryCase, obligation.case_id) if obligation.case_id else None
        if (
            risk
            and utc(demo.expires_at) > now
            and (
                case is None
                or LEAK_PRECEDENCE[LeakType(case.leak_type)]
                < LEAK_PRECEDENCE[LeakType.INVOICE_OVERDUE]
            )
        ):
            case, _ = record_risk(
                session,
                RiskSignal(
                    scope=invoice_scope(demo),
                    entity=ref,
                    obligation=ref,
                    source=source,
                    occurred_at=now,
                    leak_type=LeakType.INVOICE_OVERDUE,
                    customer_id=demo.customer_id,
                    amount_due_paise=invoice.amount_due_paise,
                    baseline_paid_paise=invoice.amount_paid_paise,
                    currency=invoice.currency,
                ),
                session_id=demo.id,
                legacy_signal=NormalizedSignal(
                    merchant_id=demo.merchant_id,
                    customer_id=demo.customer_id,
                    leak_type=LeakType.INVOICE_OVERDUE,
                    entity_type="invoice",
                    entity_id=invoice.id,
                    entity_root_id=None,
                    amount_at_risk=invoice.amount_due_paise,
                    currency=invoice.currency,
                    evidence={"source": source, "aging_bucket": "0_7"},
                    occurred_at=now,
                    dedupe_key_override=obligation.id,
                    arm_override=Arm.TREATMENT,
                ),
            )
            if case and case.outcome != "RECOVERED":
                demo.state = "AT_RISK"
        for payment in captures:
            record_payment(payment)
        obligation.outstanding_paise = invoice.amount_due_paise
        if invoice.status == "paid" and invoice.amount_paise > 0:
            record_recovery(
                session,
                RecoverySignal(
                    scope=invoice_scope(demo),
                    entity=ref,
                    obligation=ref,
                    source=source,
                    occurred_at=now,
                    currency=invoice.currency,
                    amount_due_paise=0,
                    settlement="full_settlement",
                ),
            )
            if demo.state != "EXPIRED":
                demo.state = "RECOVERED"
        demo.updated_at = now
        demo.setup_state = "READY" if decision in {"payable", "paid"} else "ACTION_REQUIRED"
        if case:
            if (
                previous.get("amount_due_paise") != invoice.amount_due_paise
                or previous.get("observed_disposition") != decision
                or previous.get("observed_status") != invoice.status
            ):
                append_event(
                    session,
                    case,
                    kind="INVOICE_RECONCILED",
                    actor="razorpay_reconciler",
                    payload={
                        "case_open": case.outcome != "RECOVERED",
                        "provider_status": invoice.status,
                        "business_overdue": now >= due_at and invoice.amount_due_paise > 0,
                        "amount_due_paise": invoice.amount_due_paise,
                        "amount_paid_paise": invoice.amount_paid_paise,
                        "disposition": decision,
                        "source": source,
                    },
                )
            if decision != "payable" or utc(demo.expires_at) <= now:
                _cancel_actions(session, case)
        entity.safe_metadata = {
            **entity.safe_metadata,
            "observed_disposition": decision,
            "observed_status": invoice.status,
        }
        if settings.mode == "live_demo":
            demo.capability_evidence = DataProvenance.LIVE_PROVIDER_VERIFIED
        _record_provider_call(
            session,
            session_id=demo.id,
            provider="razorpay",
            operation=operation,
            request_id=invoice.request_id,
            latency_ms=0,
            status="succeeded",
            metadata={
                "status": invoice.status,
                "amount_due_paise": invoice.amount_due_paise,
                "captured_payment_count": len(captures),
                "disposition": decision,
            },
        )
        if commit:
            session.commit()
        return case
    except ProviderError as exc:
        session.rollback()
        demo = session.get(DemoSession, session_id)
        entity = invoice_entity(session, demo)
        if entity is not None:
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


def invoice_recovery_token(demo: DemoSession, *, settings: Settings, now: datetime) -> str:
    from leakproof.demo.service import _signing_secret

    return issue_resource_recovery_token(
        RecoveryTokenClaims(
            version=2,
            session_id=demo.id,
            merchant_id=demo.merchant_id,
            scenario_type="INVOICE_OVERDUE",
            entity=EntityRef(entity_type="invoice", entity_id=demo.primary_entity_id),
            purpose="invoice_hosted_payment",
            amount_paise=demo.amount_paise,
            currency=demo.currency,
            expires_at=min(utc(demo.expires_at), now + timedelta(minutes=30)),
        ),
        _signing_secret(settings),
    )


def invoice_bootstrap(session: Session, claims, *, provider, settings: Settings, now: datetime):
    from leakproof.demo.contracts import InvoiceRecoveryBootstrap
    from leakproof.demo.service import RecoveryExpired, RecoveryTokenInvalid

    demo = session.get(DemoSession, claims.session_id)
    if (
        not demo
        or demo.primary_entity_type != "invoice"
        or claims.version != 2
        or claims.purpose != "invoice_hosted_payment"
        or demo.merchant_id != claims.merchant_id
        or demo.primary_entity_id != claims.entity.entity_id
        or demo.scenario_type != claims.scenario_type
        or demo.amount_paise != claims.amount_paise
        or demo.currency != claims.currency
        or demo.merchant_id != settings.default_merchant_id
        or demo.provider_mode != "test"
    ):
        raise RecoveryTokenInvalid("invalid recovery token")
    if utc(demo.expires_at) <= now or demo.state == "EXPIRED":
        raise RecoveryExpired("demo session has expired")
    provider = InvoiceReadSnapshot(provider)
    reconcile_invoice(
        session,
        demo.id,
        provider=provider,
        settings=settings,
        now=now,
        operation="recovery_invoice_check",
    )
    view = invoice_view(session, demo, now)
    # Use the same verified read and keep hosted URLs out of persisted metadata.
    invoice = provider.last_invoice
    assert invoice is not None
    decision = view["disposition"]
    return InvoiceRecoveryBootstrap(
        session_id=demo.id,
        disposition=decision,
        redirect_url=invoice.short_url if decision == "payable" else None,
        amount_due_paise=view["outstanding_balance_paise"],
        currency=demo.currency,
        expires_at=min(claims.expires_at, utc(demo.expires_at)),
    )


class InvoiceReadSnapshot:
    """Per-request wrapper, never a global cache of payment eligibility."""

    def __init__(self, provider):
        self.provider = provider
        self.last_invoice = None

    def fetch_invoice(self, invoice_id):
        self.last_invoice = self.provider.fetch_invoice(invoice_id)
        return self.last_invoice

    def list_order_payments(self, order_id):
        return self.provider.list_order_payments(order_id)


def process_invoice_webhook(
    session: Session, event, *, provider, settings: Settings, now: datetime | None = None
) -> tuple[bool, str | None]:
    """Route explicit or previously registered identities; never infer from customer/amount."""
    payload = event.payload.get("payload", {})
    invoice = payload.get("invoice", {}).get("entity", {})
    payment = payload.get("payment", {}).get("entity", {})
    order = payload.get("order", {}).get("entity", {})
    invoice_id = invoice.get("id") or payment.get("invoice_id") or order.get("invoice_id")
    order_id = payment.get("order_id") or order.get("id")
    linked = None
    if order_id:
        linked = session.scalar(
            select(ProviderEntity).where(
                ProviderEntity.merchant_id == event.merchant_id,
                ProviderEntity.mode == "test",
                ProviderEntity.provider == "razorpay",
                ProviderEntity.entity_type == "order",
                ProviderEntity.provider_entity_id == order_id,
                ProviderEntity.root_entity_type == "invoice",
            )
        )
    invoice_id = invoice_id or (linked.root_entity_id if linked else None)
    if not invoice_id:
        # A cross-merchant order must not enter the legacy amount/customer fallback.
        other = (
            session.scalar(
                select(ProviderEntity.id).where(
                    ProviderEntity.provider_entity_id == order_id,
                    ProviderEntity.root_entity_type == "invoice",
                )
            )
            if order_id
            else None
        )
        return bool(event.event_type.startswith("invoice.") or other), None
    if event.merchant_id != settings.default_merchant_id:
        return True, None
    demo = session.scalar(
        select(DemoSession).where(
            DemoSession.merchant_id == event.merchant_id,
            DemoSession.provider_mode == "test",
            DemoSession.primary_entity_type == "invoice",
            DemoSession.primary_entity_id == invoice_id,
        )
    )
    if demo is None:
        return True, None
    case = reconcile_invoice(
        session, demo.id, provider=provider, settings=settings, now=now, source="razorpay_webhook"
    )
    return True, case.id if case else None


def reconcile_invoice_sessions(
    *, session_factory, provider, settings: Settings, now: datetime | None = None, limit: int = 100
) -> dict:
    now = utc(now or datetime.now(UTC))
    with session_factory() as session:
        # Oldest checked first, so provider failures cannot starve another invoice.
        rows = list(
            session.scalars(
                select(ProviderEntity)
                .where(
                    ProviderEntity.merchant_id == settings.default_merchant_id,
                    ProviderEntity.provider == "razorpay",
                    ProviderEntity.mode == "test",
                    ProviderEntity.entity_type == "invoice",
                    ProviderEntity.session_id.is_not(None),
                )
                .order_by(ProviderEntity.state_observed_at.asc().nullsfirst(), ProviderEntity.id)
            )
        )
        ids = [
            r.session_id
            for r in rows
            if r.state_observed_at is None
            or utc(r.state_observed_at)
            <= now - timedelta(seconds=settings.invoice_reconcile_seconds)
        ][:limit]
    scanned = failed = 0
    cases = []
    for session_id in ids:
        with session_factory() as session:
            try:
                case = reconcile_invoice(
                    session, session_id, provider=provider, settings=settings, now=now
                )
                if case:
                    from leakproof.demo.email import schedule_demo_recovery_email
                    from leakproof.diagnosis import diagnose_case

                    cases.append(case.id)
                    diagnose_case(session, case.id)
                    demo = session.get(DemoSession, session_id)
                    view = invoice_view(session, demo, now)
                    if (
                        view["disposition"] == "payable"
                        and case.outcome != "RECOVERED"
                        and utc(demo.expires_at) > now
                    ):
                        schedule_demo_recovery_email(session, case.id, settings=settings, now=now)
                    session.commit()
            except ProviderError:
                failed += 1
            scanned += 1
    return {
        "sensor": "invoice_aging",
        "scanned": scanned,
        "signals": len(set(cases)),
        "failed": failed,
    }
