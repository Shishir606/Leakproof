from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from leakproof.config import Settings
from leakproof.demo.contracts import (
    CheckoutEventReceipt,
    CheckoutEventRequest,
    CheckoutEventType,
    CheckoutPaymentVerificationReceipt,
    CheckoutPaymentVerificationRequest,
    DemoSessionCreated,
    DemoSessionCreateRequest,
    DemoSessionState,
    EmailMode,
    InvoiceRecoveryBootstrap,
    InvoiceSessionCreated,
    RecoveryBootstrap,
    SubscriptionRecoveryBootstrap,
    SubscriptionSessionCreated,
    assert_session_transition,
    live_case_dedupe_key,
)
from leakproof.demo.rate_limit import InMemoryRateLimiter, RedisRateLimiter
from leakproof.demo.security import (
    InvalidCheckoutPaymentSignature,
    InvalidRecoveryToken,
    InvalidSessionToken,
    RecoveryTokenExpired,
    SessionTokenExpired,
    encrypt_recipient,
    issue_recovery_token,
    issue_session_token,
    recipient_hash,
    verify_checkout_payment_signature,
    verify_recovery_token,
    verify_session_token,
)
from leakproof.models.db import (
    CheckoutEvent,
    Customer,
    DemoSession,
    Merchant,
    ProviderCall,
    RecoveryCase,
)
from leakproof.models.domain import Arm, LeakType
from leakproof.models.resources import ObligationRef, ProviderScope, RecoveryPurpose
from leakproof.provenance import DataProvenance
from leakproof.provider_resources import (
    get_obligation,
    needs_payment_reconciliation,
    order_recovery_supported,
    register_entity,
)
from leakproof.providers import CreateOrderRequest, PaymentProvider, ProviderError
from leakproof.sensors.processor import promote_abandonment_case
from leakproof.services import (
    NormalizedSignal,
    PaidSignal,
    new_id,
    record_paid_signal,
    record_signal,
)

RateLimiter = RedisRateLimiter | InMemoryRateLimiter


class DemoSessionUnauthorized(ValueError):
    pass


class DemoSessionExpired(ValueError):
    pass


class RecoveryTokenInvalid(ValueError):
    pass


class RecoveryExpired(ValueError):
    pass


class RecoveryOrderNotAvailable(ValueError):
    pass


class CheckoutPaymentProofInvalid(ValueError):
    pass


class CheckoutPaymentNotCaptured(ValueError):
    pass


class CheckoutPaymentVerificationUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class DemoRateLimitExceeded(RuntimeError):
    retry_after_seconds: int
    scope: str


@dataclass(frozen=True)
class CheckoutEventIngested:
    receipt: CheckoutEventReceipt
    dismissal_event_id: int | None = None


def _record_checkout_verification(
    session: Session,
    *,
    session_id: str,
    status: str,
    latency_ms: int,
    payment_status: str | None = None,
    request_id: str | None = None,
    error_class: str | None = None,
) -> None:
    metadata: dict[str, str | bool] = {
        "verification_source": "checkout_signature_plus_payment_api",
        "signature_checked_server_side": True,
    }
    if payment_status:
        metadata["payment_status"] = payment_status
    _record_provider_call(
        session,
        session_id=session_id,
        provider="razorpay",
        operation="verify_checkout_payment",
        request_id=request_id,
        latency_ms=latency_ms,
        status=status,
        metadata=metadata,
        error_class=error_class,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _signing_secret(settings: Settings) -> str:
    return settings.recovery_token_secret or "leakproof-simulation-only-signing-secret"


def _subject_hash(value: str, secret: str) -> str:
    return hashlib.sha256(f"{secret}:ip-rate-limit:{value}".encode()).hexdigest()


def _record_provider_call(
    session: Session,
    *,
    session_id: str | None,
    provider: str,
    operation: str,
    request_id: str | None,
    latency_ms: int,
    status: str,
    metadata: dict,
    error_class: str | None = None,
) -> None:
    session.add(
        ProviderCall(
            session_id=session_id,
            provider=provider,
            operation=operation,
            request_id=request_id,
            safe_response_metadata=metadata,
            latency_ms=max(0, latency_ms),
            attempt_number=1,
            status=status,
            error_class=error_class,
        )
    )


def create_demo_session(
    session: Session,
    request: DemoSessionCreateRequest,
    *,
    client_ip: str,
    provider: PaymentProvider,
    limiter: RateLimiter,
    settings: Settings,
    now: datetime | None = None,
) -> DemoSessionCreated | InvoiceSessionCreated | SubscriptionSessionCreated:
    if request.scenario_type not in {
        LeakType.PAYMENT_FAILURE,
        LeakType.CHECKOUT_ABANDON,
        LeakType.INVOICE_OVERDUE,
        LeakType.SUBSCRIPTION_HALT,
    }:
        raise ValueError("scenario_not_implemented")
    now = _as_utc(now or datetime.now(UTC))
    secret = _signing_secret(settings)
    decision = limiter.allow(
        "demo-sessions-ip",
        _subject_hash(client_ip, secret),
        limit=settings.demo_sessions_per_ip_hour,
        window_seconds=3_600,
        now=now.timestamp(),
    )
    if not decision.allowed:
        raise DemoRateLimitExceeded(decision.retry_after_seconds, "sessions_per_ip")

    if request.scenario_type == LeakType.INVOICE_OVERDUE:
        from leakproof.demo.invoices import create_invoice_session

        return create_invoice_session(
            session, request, provider=provider, settings=settings, now=now
        )
    if request.scenario_type == LeakType.SUBSCRIPTION_HALT:
        from leakproof.demo.subscriptions import create_subscription_session

        return create_subscription_session(
            session, request, provider=provider, settings=settings, now=now
        )

    session_id = new_id("demo")
    customer_id = new_id("demo_customer")
    merchant_id = settings.default_merchant_id
    expires_at = now + timedelta(minutes=settings.demo_session_ttl_minutes)
    order_request = CreateOrderRequest(
        amount_paise=settings.demo_amount_paise,
        currency=settings.demo_currency,
        receipt=f"demo-{session_id}"[:40],
        idempotency_key=f"demo-order:{session_id}",
        notes={
            "session_id": session_id,
            "customer_id": customer_id,
            "merchant_id": merchant_id,
        },
    )

    started = time.perf_counter()
    try:
        order = provider.create_order(order_request)
    except ProviderError as exc:
        session.rollback()
        _record_provider_call(
            session,
            session_id=None,
            provider="razorpay",
            operation="create_order",
            request_id=exc.request_id,
            latency_ms=round((time.perf_counter() - started) * 1000),
            status="failed",
            metadata={
                "amount_paise": settings.demo_amount_paise,
                "currency": settings.demo_currency,
            },
            error_class=exc.error_class,
        )
        session.commit()
        raise
    if order.status != "created":
        raise ProviderError(
            provider="razorpay",
            operation="create_order",
            error_class="unexpected_order_state",
            retryable=False,
            message="Razorpay created an order in an unexpected state",
            request_id=order.request_id,
        )

    if session.get(Merchant, merchant_id) is None:
        session.add(Merchant(id=merchant_id, name=merchant_id, policy={}))
        session.flush()
    session.add(Customer(id=customer_id, merchant_id=merchant_id))

    normalized_recipient = request.recipient
    allowlisted = (
        normalized_recipient is not None and normalized_recipient in settings.allowed_demo_emails
    )
    demo = DemoSession(
        id=session_id,
        merchant_id=merchant_id,
        customer_id=customer_id,
        razorpay_order_id=order.id,
        scenario_type=request.scenario_type,
        primary_entity_type="order",
        primary_entity_id=order.id,
        provider_mode="test",
        setup_state="READY",
        capability_evidence=(
            DataProvenance.ARCHITECTURE_READY
            if settings.mode == "live_demo"
            else DataProvenance.SIMULATED_END_TO_END
        ),
        amount_paise=settings.demo_amount_paise,
        currency=settings.demo_currency,
        state=DemoSessionState.CREATED.value,
        recipient_ciphertext=(
            encrypt_recipient(normalized_recipient, secret) if allowlisted else None
        ),
        recipient_hash=(
            recipient_hash(normalized_recipient, secret) if normalized_recipient else None
        ),
        expires_at=expires_at,
        created_at=now,
        updated_at=now,
    )
    session.add(demo)
    session.flush()
    scope = ProviderScope(merchant_id=merchant_id)
    ref = ObligationRef(entity_type="order", entity_id=order.id)
    obligation = get_obligation(session, scope, ref, demo.currency)
    register_entity(session, scope, ref, session_id=demo.id, obligation=obligation, role="primary")
    _record_provider_call(
        session,
        session_id=session_id,
        provider="razorpay",
        operation="create_order",
        request_id=order.request_id,
        latency_ms=round((time.perf_counter() - started) * 1000),
        status="succeeded",
        metadata={
            "amount_paise": order.amount_paise,
            "currency": order.currency,
            "order_status": order.status,
        },
    )
    session.commit()

    return DemoSessionCreated(
        scenario_type=request.scenario_type,
        session_id=session_id,
        session_token=issue_session_token(session_id, merchant_id, expires_at, secret),
        razorpay_key_id=settings.razorpay_key_id or "rzp_test_simulated",
        razorpay_order_id=order.id,
        amount_paise=demo.amount_paise,
        currency=demo.currency,
        expires_at=expires_at,
        email_mode=(EmailMode.ALLOWLISTED if allowlisted else EmailMode.PREVIEW_ONLY),
    )


def issue_demo_recovery_token(
    session: Session,
    session_id: str,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> str:
    """Issue a short-lived, fully bound token only for an active recovery session."""
    now = _as_utc(now or datetime.now(UTC))
    demo = session.get(DemoSession, session_id)
    if demo is None:
        raise RecoveryTokenInvalid("recovery session was not found")
    if _as_utc(demo.expires_at) <= now:
        raise RecoveryExpired("demo session has expired")
    if DemoSessionState(demo.state) not in {
        DemoSessionState.AT_RISK,
        DemoSessionState.CHECKOUT_OPEN,
    }:
        raise RecoveryOrderNotAvailable("the order is not available for recovery")
    if demo.primary_entity_type == "invoice":
        from leakproof.demo.invoices import invoice_recovery_token

        return invoice_recovery_token(demo, settings=settings, now=now)
    if demo.primary_entity_type == "subscription":
        from leakproof.demo.subscriptions import subscription_recovery_token

        return subscription_recovery_token(demo, settings=settings, now=now)
    if not order_recovery_supported(session, demo):
        raise RecoveryOrderNotAvailable("this recovery surface is not implemented")
    return issue_recovery_token(
        demo.id,
        demo.merchant_id,
        demo.razorpay_order_id,
        demo.amount_paise,
        demo.currency,
        now + timedelta(minutes=30),
        _signing_secret(settings),
        scenario_type=LeakType(demo.scenario_type),
    )


def get_recovery_bootstrap(
    session: Session,
    signed_token: str,
    *,
    provider: PaymentProvider,
    settings: Settings,
    now: datetime | None = None,
) -> RecoveryBootstrap | InvoiceRecoveryBootstrap | SubscriptionRecoveryBootstrap:
    """Verify all token bindings and re-read payment truth before reopening Checkout."""
    now = _as_utc(now or datetime.now(UTC))
    try:
        claims = verify_recovery_token(
            signed_token,
            _signing_secret(settings),
            now=now,
        )
    except RecoveryTokenExpired as exc:
        raise RecoveryExpired("recovery token has expired") from exc
    except InvalidRecoveryToken as exc:
        raise RecoveryTokenInvalid("invalid recovery token") from exc

    if claims.purpose == RecoveryPurpose.INVOICE_HOSTED_PAYMENT:
        from leakproof.demo.invoices import invoice_bootstrap

        return invoice_bootstrap(session, claims, provider=provider, settings=settings, now=now)
    if claims.purpose == RecoveryPurpose.SUBSCRIPTION_METHOD_UPDATE:
        from leakproof.demo.subscriptions import subscription_bootstrap

        return subscription_bootstrap(
            session, claims, provider=provider, settings=settings, now=now
        )
    if claims.purpose != RecoveryPurpose.ORDER_CHECKOUT:
        raise RecoveryTokenInvalid("invalid recovery token purpose")

    demo = session.scalar(
        select(DemoSession).where(DemoSession.id == claims.session_id).with_for_update()
    )
    if demo is None or (
        demo.merchant_id != claims.merchant_id
        or not order_recovery_supported(session, demo)
        or demo.primary_entity_id != claims.entity.entity_id
        or (claims.version == 2 and demo.scenario_type != claims.scenario_type)
        or demo.razorpay_order_id != claims.order_id
        or demo.amount_paise != claims.amount_paise
        or demo.currency != claims.currency
    ):
        raise RecoveryTokenInvalid("invalid recovery token")
    if _as_utc(demo.expires_at) <= now or DemoSessionState(demo.state) == DemoSessionState.EXPIRED:
        if DemoSessionState(demo.state) != DemoSessionState.EXPIRED:
            demo.state = DemoSessionState.EXPIRED.value
            demo.updated_at = now
            session.commit()
        raise RecoveryExpired("demo session has expired")
    if DemoSessionState(demo.state) == DemoSessionState.RECOVERED:
        raise RecoveryOrderNotAvailable("the order has already been paid")
    if DemoSessionState(demo.state) not in {
        DemoSessionState.AT_RISK,
        DemoSessionState.CHECKOUT_OPEN,
    }:
        raise RecoveryOrderNotAvailable("the order is not available for recovery")

    started = time.perf_counter()
    try:
        payments = provider.list_order_payments(demo.razorpay_order_id)
    except ProviderError as exc:
        session.rollback()
        _record_provider_call(
            session,
            session_id=demo.id,
            provider="razorpay",
            operation="recovery_order_check",
            request_id=exc.request_id,
            latency_ms=round((time.perf_counter() - started) * 1000),
            status="failed",
            metadata={},
            error_class=exc.error_class,
        )
        session.commit()
        raise

    if any(
        payment.order_id != demo.razorpay_order_id
        or payment.amount_paise != demo.amount_paise
        or payment.currency != demo.currency
        for payment in payments
    ):
        raise ProviderError(
            provider="razorpay",
            operation="recovery_order_check",
            error_class="response_mismatch",
            retryable=False,
            message="Razorpay returned a payment for another order",
        )
    _record_provider_call(
        session,
        session_id=demo.id,
        provider="razorpay",
        operation="recovery_order_check",
        request_id=next((item.request_id for item in payments if item.request_id), None),
        latency_ms=round((time.perf_counter() - started) * 1000),
        status="succeeded",
        metadata={
            "payment_count": len(payments),
            "statuses": sorted({item.status for item in payments}),
        },
    )
    captured = next((payment for payment in payments if payment.status == "captured"), None)
    if captured:
        reconcile_demo_capture(session, demo, captured, now)
    if any(payment.status != "failed" for payment in payments):
        session.commit()
        raise RecoveryOrderNotAvailable("the order is no longer available for recovery")

    assert_session_transition(DemoSessionState(demo.state), DemoSessionState.CHECKOUT_OPEN)
    demo.state = DemoSessionState.CHECKOUT_OPEN.value
    demo.updated_at = now
    session.commit()
    return RecoveryBootstrap(
        session_id=demo.id,
        razorpay_key_id=settings.razorpay_key_id or "rzp_test_simulated",
        razorpay_order_id=demo.razorpay_order_id,
        amount_paise=demo.amount_paise,
        currency=demo.currency,
        expires_at=min(claims.expires_at, _as_utc(demo.expires_at)),
    )


def ingest_checkout_event(
    session: Session,
    session_id: str,
    request: CheckoutEventRequest,
    *,
    session_token: str,
    limiter: RateLimiter,
    settings: Settings,
    now: datetime | None = None,
) -> CheckoutEventIngested:
    now = _as_utc(now or datetime.now(UTC))
    secret = _signing_secret(settings)
    try:
        claims = verify_session_token(session_token, secret, now=now)
    except SessionTokenExpired as exc:
        raise DemoSessionExpired("demo session has expired") from exc
    except InvalidSessionToken as exc:
        raise DemoSessionUnauthorized("invalid session token") from exc
    if claims.session_id != session_id:
        raise DemoSessionUnauthorized("session token does not match the requested session")

    demo = session.scalar(select(DemoSession).where(DemoSession.id == session_id).with_for_update())
    if demo is None or claims.merchant_id != demo.merchant_id:
        raise DemoSessionUnauthorized("invalid session token")
    if not order_recovery_supported(session, demo):
        raise DemoSessionUnauthorized("session does not support order checkout")
    if _as_utc(demo.expires_at) <= now or DemoSessionState(demo.state) == DemoSessionState.EXPIRED:
        if DemoSessionState(demo.state) != DemoSessionState.EXPIRED:
            demo.state = DemoSessionState.EXPIRED.value
            demo.updated_at = now
            session.commit()
        raise DemoSessionExpired("demo session has expired")
    if DemoSessionState(demo.state) == DemoSessionState.RECOVERED:
        raise DemoSessionExpired("demo session is already complete")

    existing = session.scalar(
        select(CheckoutEvent).where(
            CheckoutEvent.session_id == session_id,
            CheckoutEvent.client_event_id == request.client_event_id,
        )
    )
    if existing is not None:
        return CheckoutEventIngested(
            receipt=CheckoutEventReceipt(duplicate=True, event_id=existing.id)
        )

    count = int(
        session.scalar(
            select(func.count(CheckoutEvent.id)).where(CheckoutEvent.session_id == session_id)
        )
        or 0
    )
    if count >= settings.demo_checkout_events_per_session:
        raise DemoRateLimitExceeded(0, "checkout_events_per_session")
    decision = limiter.allow(
        "demo-checkout-events",
        session_id,
        limit=settings.demo_checkout_events_per_session,
        window_seconds=settings.demo_session_ttl_minutes * 60,
        member=request.client_event_id,
        now=now.timestamp(),
    )
    if not decision.allowed:
        raise DemoRateLimitExceeded(decision.retry_after_seconds, "checkout_events_per_session")

    event = CheckoutEvent(
        session_id=session_id,
        client_event_id=request.client_event_id,
        event_type=request.event_type.value,
        occurred_at=request.occurred_at,
        received_at=now,
        event_metadata=request.metadata.model_dump(exclude_none=True),
    )
    session.add(event)
    if request.event_type in {
        CheckoutEventType.CHECKOUT_OPENED,
        CheckoutEventType.PAYMENT_ATTEMPT_STARTED,
    }:
        target = DemoSessionState.CHECKOUT_OPEN
        assert_session_transition(DemoSessionState(demo.state), target)
        demo.state = target.value
        demo.updated_at = now
    session.commit()
    return CheckoutEventIngested(
        receipt=CheckoutEventReceipt(duplicate=False, event_id=event.id),
        dismissal_event_id=(
            event.id if request.event_type == CheckoutEventType.CHECKOUT_DISMISSED else None
        ),
    )


def verify_checkout_payment(
    session: Session,
    session_id: str,
    request: CheckoutPaymentVerificationRequest,
    *,
    provider: PaymentProvider,
    limiter: RateLimiter,
    settings: Settings,
    session_token: str = "",
    recovery_token: str = "",
    now: datetime | None = None,
) -> CheckoutPaymentVerificationReceipt:
    """Close a demo only after Razorpay signature and captured-payment API verification."""
    now = _as_utc(now or datetime.now(UTC))
    if not settings.razorpay_key_id.startswith("rzp_test_") or not settings.razorpay_key_secret:
        raise CheckoutPaymentVerificationUnavailable(
            "Razorpay test-mode payment verification is not configured"
        )

    secret = _signing_secret(settings)
    if session_token:
        try:
            claims = verify_session_token(session_token, secret, now=now)
        except SessionTokenExpired as exc:
            raise DemoSessionExpired("demo session has expired") from exc
        except InvalidSessionToken as exc:
            raise DemoSessionUnauthorized("invalid session token") from exc
        if claims.session_id != session_id:
            raise DemoSessionUnauthorized("session token does not match the requested session")
        authorized_merchant_id = claims.merchant_id
        recovery_claims = None
    elif recovery_token:
        try:
            recovery_claims = verify_recovery_token(
                recovery_token, secret, now=now, expected_purpose=RecoveryPurpose.ORDER_CHECKOUT
            )
        except RecoveryTokenExpired as exc:
            raise DemoSessionExpired("recovery token has expired") from exc
        except InvalidRecoveryToken as exc:
            raise DemoSessionUnauthorized("invalid recovery token") from exc
        if recovery_claims.session_id != session_id:
            raise DemoSessionUnauthorized("recovery token does not match the requested session")
        authorized_merchant_id = recovery_claims.merchant_id
    else:
        raise DemoSessionUnauthorized("payment verification token is required")

    demo = session.scalar(select(DemoSession).where(DemoSession.id == session_id).with_for_update())
    if demo is None or demo.merchant_id != authorized_merchant_id:
        raise DemoSessionUnauthorized("invalid payment verification token")
    if not order_recovery_supported(session, demo):
        raise DemoSessionUnauthorized("session does not support order checkout")
    if recovery_claims is not None and (
        (recovery_claims.version == 2 and recovery_claims.scenario_type != demo.scenario_type)
        or recovery_claims.entity.entity_id != demo.primary_entity_id
        or recovery_claims.order_id != demo.razorpay_order_id
        or recovery_claims.amount_paise != demo.amount_paise
        or recovery_claims.currency != demo.currency
    ):
        raise DemoSessionUnauthorized("invalid payment verification token")
    if (
        _as_utc(demo.expires_at) <= now
        and DemoSessionState(demo.state) != DemoSessionState.RECOVERED
    ):
        raise DemoSessionExpired("demo session has expired")

    decision = limiter.allow(
        "demo-payment-verifications",
        session_id,
        limit=10,
        window_seconds=settings.demo_session_ttl_minutes * 60,
        now=now.timestamp(),
    )
    if not decision.allowed:
        raise DemoRateLimitExceeded(decision.retry_after_seconds, "payment_verifications")

    started = time.perf_counter()
    if request.razorpay_order_id != demo.razorpay_order_id:
        _record_checkout_verification(
            session,
            session_id=demo.id,
            status="failed",
            latency_ms=round((time.perf_counter() - started) * 1000),
            error_class="order_mismatch",
        )
        session.commit()
        raise CheckoutPaymentProofInvalid("checkout payment proof is invalid")
    try:
        verify_checkout_payment_signature(
            demo.razorpay_order_id,
            request.razorpay_payment_id,
            request.razorpay_signature,
            settings.razorpay_key_secret,
        )
    except InvalidCheckoutPaymentSignature as exc:
        _record_checkout_verification(
            session,
            session_id=demo.id,
            status="failed",
            latency_ms=round((time.perf_counter() - started) * 1000),
            error_class="signature_invalid",
        )
        session.commit()
        raise CheckoutPaymentProofInvalid("checkout payment proof is invalid") from exc

    if DemoSessionState(
        demo.state
    ) == DemoSessionState.RECOVERED and not needs_payment_reconciliation(session, demo):
        return CheckoutPaymentVerificationReceipt(duplicate=True)

    try:
        payment = provider.fetch_payment(request.razorpay_payment_id)
    except ProviderError as exc:
        session.rollback()
        _record_checkout_verification(
            session,
            session_id=session_id,
            status="failed",
            latency_ms=round((time.perf_counter() - started) * 1000),
            request_id=exc.request_id,
            error_class=exc.error_class,
        )
        session.commit()
        raise

    if (
        payment.id != request.razorpay_payment_id
        or payment.order_id != demo.razorpay_order_id
        or payment.amount_paise != demo.amount_paise
        or payment.currency != demo.currency
    ):
        _record_checkout_verification(
            session,
            session_id=demo.id,
            status="failed",
            latency_ms=round((time.perf_counter() - started) * 1000),
            payment_status=payment.status,
            request_id=payment.request_id,
            error_class="response_mismatch",
        )
        session.commit()
        raise CheckoutPaymentProofInvalid("Razorpay payment did not match the demo order")
    if payment.status != "captured":
        _record_checkout_verification(
            session,
            session_id=demo.id,
            status="pending",
            latency_ms=round((time.perf_counter() - started) * 1000),
            payment_status=payment.status,
            request_id=payment.request_id,
            error_class="payment_not_captured",
        )
        session.commit()
        raise CheckoutPaymentNotCaptured("Razorpay has not captured the payment yet")

    _record_checkout_verification(
        session,
        session_id=demo.id,
        status="succeeded",
        latency_ms=round((time.perf_counter() - started) * 1000),
        payment_status=payment.status,
        request_id=payment.request_id,
    )
    record_paid_signal(
        session,
        PaidSignal(
            merchant_id=demo.merchant_id,
            customer_id=demo.customer_id,
            entity_id=payment.id,
            entity_root_id=demo.razorpay_order_id,
            amount_paise=payment.amount_paise,
            currency=payment.currency,
            evidence={
                "source": "razorpay_checkout_signature_and_api",
                "signature_verified": True,
                "payment_status": payment.status,
            },
            occurred_at=now,
        ),
    )
    assert_session_transition(DemoSessionState(demo.state), DemoSessionState.RECOVERED)
    demo.state = DemoSessionState.RECOVERED.value
    demo.updated_at = now
    session.commit()
    return CheckoutPaymentVerificationReceipt(duplicate=False)


def materialize_checkout_abandonment(
    session: Session,
    session_id: str,
    dismissal_event_id: int,
    *,
    provider: PaymentProvider,
    settings: Settings,
    now: datetime | None = None,
) -> str | None:
    now = _as_utc(now or datetime.now(UTC))
    demo = session.scalar(select(DemoSession).where(DemoSession.id == session_id).with_for_update())
    if demo is None or not order_recovery_supported(session, demo):
        return None
    state = DemoSessionState(demo.state)
    if state in {DemoSessionState.RECOVERED, DemoSessionState.EXPIRED}:
        return None
    if _as_utc(demo.expires_at) <= now:
        demo.state = DemoSessionState.EXPIRED.value
        demo.updated_at = now
        session.commit()
        return None

    dismissal = session.scalar(
        select(CheckoutEvent).where(
            CheckoutEvent.id == dismissal_event_id,
            CheckoutEvent.session_id == session_id,
            CheckoutEvent.event_type == CheckoutEventType.CHECKOUT_DISMISSED.value,
        )
    )
    if dismissal is None:
        return None
    due_at = _as_utc(dismissal.received_at) + timedelta(
        seconds=settings.demo_abandonment_delay_seconds
    )
    if now < due_at:
        return None
    # Server receipt order (including equal timestamps) is authoritative. Reopening,
    # a new attempt, completion, or a newer dismissal supersedes this timer.
    latest = latest_checkout_event(session, session_id)
    if latest is None or latest.id != dismissal.id:
        return None

    key = live_case_dedupe_key(session_id, demo.razorpay_order_id)
    existing = session.scalar(
        select(RecoveryCase).where(
            RecoveryCase.merchant_id == demo.merchant_id,
            or_(
                RecoveryCase.dedupe_key == key,
                (
                    (RecoveryCase.customer_id == demo.customer_id)
                    & (RecoveryCase.leak_type == LeakType.PAYMENT_FAILURE.value)
                    & (RecoveryCase.dedupe_key == f"pf:{demo.customer_id}:{demo.razorpay_order_id}")
                ),
            ),
        )
    )
    if existing is not None and existing.leak_type == LeakType.PAYMENT_FAILURE.value:
        demo.state = DemoSessionState.AT_RISK.value
        demo.updated_at = now
        session.commit()
        return existing.id

    checked = session.scalar(
        select(ProviderCall.id).where(
            ProviderCall.session_id == demo.id,
            ProviderCall.operation == "list_order_payments",
            ProviderCall.status == "succeeded",
            ProviderCall.safe_response_metadata["dismissal_event_id"].as_integer() == dismissal.id,
            ProviderCall.safe_response_metadata["check_resolved"].as_boolean().is_(True),
        )
    )
    if checked is not None:
        return existing.id if existing else None

    started = time.perf_counter()
    try:
        payments = provider.list_order_payments(demo.razorpay_order_id)
        if any(
            payment.order_id != demo.razorpay_order_id
            or payment.amount_paise != demo.amount_paise
            or payment.currency != demo.currency
            for payment in payments
        ):
            raise ProviderError(
                provider="razorpay",
                operation="list_order_payments",
                error_class="response_mismatch",
                retryable=False,
                message="Razorpay payment state did not match the demo order",
            )
    except ProviderError as exc:
        _record_provider_call(
            session,
            session_id=demo.id,
            provider="razorpay",
            operation="list_order_payments",
            request_id=exc.request_id,
            latency_ms=round((time.perf_counter() - started) * 1000),
            status="failed",
            metadata={"dismissal_event_id": dismissal.id},
            error_class=exc.error_class,
        )
        session.commit()
        raise
    pending = any(payment.status not in {"captured", "failed"} for payment in payments)
    captured = next((payment for payment in payments if payment.status == "captured"), None)
    _record_provider_call(
        session,
        session_id=demo.id,
        provider="razorpay",
        operation="list_order_payments",
        request_id=next((item.request_id for item in payments if item.request_id), None),
        latency_ms=round((time.perf_counter() - started) * 1000),
        status="succeeded",
        metadata={
            "dismissal_event_id": dismissal.id,
            "payment_count": len(payments),
            "statuses": sorted({item.status for item in payments}),
            "check_resolved": not pending or captured is not None,
            "unpaid_confirmed": not payments,
        },
    )
    if captured is not None:
        # Provider capture is authoritative even when the browser callback/webhook was lost.
        reconcile_demo_capture(session, demo, captured, now)
        session.commit()
        return None
    if pending:
        session.commit()
        return None
    failed_payment = next(
        (payment for payment in reversed(payments) if payment.status == "failed"), None
    )
    if failed_payment is not None:
        signal = NormalizedSignal(
            merchant_id=demo.merchant_id,
            customer_id=demo.customer_id,
            leak_type=LeakType.PAYMENT_FAILURE,
            entity_type="payment",
            entity_id=failed_payment.id,
            entity_root_id=demo.razorpay_order_id,
            amount_at_risk=demo.amount_paise,
            currency=demo.currency,
            evidence={
                "source": "razorpay_payment_api",
                "error_reason": "provider_reported_failed",
                "method": failed_payment.method or "unknown",
                "payment_status": failed_payment.status,
            },
            occurred_at=now,
            dedupe_key_override=key,
            arm_override=Arm.TREATMENT,
        )
    elif existing is not None:
        # A repeated dismissal without new payment failure retains the original audit.
        demo.state = DemoSessionState.AT_RISK.value
        demo.updated_at = now
        session.commit()
        return existing.id
    else:
        signal = NormalizedSignal(
            merchant_id=demo.merchant_id,
            customer_id=demo.customer_id,
            leak_type=LeakType.CHECKOUT_ABANDON,
            entity_type="order",
            entity_id=demo.razorpay_order_id,
            entity_root_id=demo.razorpay_order_id,
            amount_at_risk=demo.amount_paise,
            currency=demo.currency,
            evidence={
                "source": "browser_telemetry",
                "error_reason": "checkout_abandoned",
                "session_id": demo.id,
                "dismissal_event_id": dismissal.id,
                "dismissed_at": _as_utc(dismissal.received_at).isoformat(),
                "inactivity_seconds": settings.demo_abandonment_delay_seconds,
            },
            occurred_at=now,
            dedupe_key_override=key,
            arm_override=Arm.TREATMENT,
        )
    case, _ = record_signal(session, signal)
    if case is None:
        session.commit()
        return None
    if signal.leak_type == LeakType.PAYMENT_FAILURE:
        promote_abandonment_case(session, case, signal)
    assert_session_transition(DemoSessionState(demo.state), DemoSessionState.AT_RISK)
    demo.state = DemoSessionState.AT_RISK.value
    demo.updated_at = now
    session.commit()
    return case.id


def reconcile_demo_capture(session: Session, demo: DemoSession, payment, now: datetime) -> None:
    record_paid_signal(
        session,
        PaidSignal(
            merchant_id=demo.merchant_id,
            customer_id=demo.customer_id,
            entity_id=payment.id,
            entity_root_id=demo.razorpay_order_id,
            amount_paise=payment.amount_paise,
            currency=payment.currency,
            evidence={"source": "razorpay_payment_api", "payment_status": "captured"},
            occurred_at=now,
        ),
    )
    demo.state = DemoSessionState.RECOVERED.value
    demo.updated_at = now


def latest_checkout_event(session: Session, session_id: str) -> CheckoutEvent | None:
    return session.scalar(
        select(CheckoutEvent)
        .where(CheckoutEvent.session_id == session_id)
        .order_by(CheckoutEvent.id.desc())
        .limit(1)
    )


def due_abandonment_checks(
    session: Session,
    *,
    settings: Settings,
    now: datetime | None = None,
    limit: int = 100,
) -> list[tuple[str, int]]:
    now = _as_utc(now or datetime.now(UTC))
    later = aliased(CheckoutEvent)
    dismissal = aliased(CheckoutEvent)
    cutoff = now - timedelta(seconds=settings.demo_abandonment_delay_seconds)
    rows = session.execute(
        select(dismissal.session_id, dismissal.id)
        .join(DemoSession, DemoSession.id == dismissal.session_id)
        .where(
            dismissal.event_type == CheckoutEventType.CHECKOUT_DISMISSED.value,
            dismissal.received_at <= cutoff,
            DemoSession.state.in_(["CREATED", "CHECKOUT_OPEN", "AT_RISK"]),
            DemoSession.expires_at > now,
            ~select(later.id)
            .where(
                later.session_id == dismissal.session_id,
                later.id > dismissal.id,
            )
            .exists(),
            ~select(ProviderCall.id)
            .where(
                ProviderCall.session_id == dismissal.session_id,
                ProviderCall.operation == "list_order_payments",
                ProviderCall.status == "succeeded",
                ProviderCall.safe_response_metadata["dismissal_event_id"].as_integer()
                == dismissal.id,
                ProviderCall.safe_response_metadata["check_resolved"].as_boolean().is_(True),
            )
            .exists(),
            ~select(RecoveryCase.id)
            .where(
                RecoveryCase.merchant_id == DemoSession.merchant_id,
                or_(
                    RecoveryCase.dedupe_key
                    == "live:" + DemoSession.id + ":" + DemoSession.razorpay_order_id,
                    RecoveryCase.dedupe_key
                    == "pf:" + DemoSession.customer_id + ":" + DemoSession.razorpay_order_id,
                ),
                RecoveryCase.leak_type == "PAYMENT_FAILURE",
            )
            .exists(),
        )
        .order_by(dismissal.received_at, dismissal.id)
        .limit(limit)
    ).all()
    return [(str(session_id), int(event_id)) for session_id, event_id in rows]
