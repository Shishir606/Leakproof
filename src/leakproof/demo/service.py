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
    DemoSessionCreated,
    DemoSessionCreateRequest,
    DemoSessionState,
    EmailMode,
    assert_session_transition,
    live_case_dedupe_key,
)
from leakproof.demo.rate_limit import InMemoryRateLimiter, RedisRateLimiter
from leakproof.demo.security import (
    InvalidSessionToken,
    SessionTokenExpired,
    encrypt_recipient,
    issue_session_token,
    recipient_hash,
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
from leakproof.providers import CreateOrderRequest, PaymentProvider, ProviderError
from leakproof.services import NormalizedSignal, new_id, record_signal

RateLimiter = RedisRateLimiter | InMemoryRateLimiter


class DemoSessionUnauthorized(ValueError):
    pass


class DemoSessionExpired(ValueError):
    pass


@dataclass(frozen=True)
class DemoRateLimitExceeded(RuntimeError):
    retry_after_seconds: int
    scope: str


@dataclass(frozen=True)
class CheckoutEventIngested:
    receipt: CheckoutEventReceipt
    dismissal_event_id: int | None = None


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
) -> DemoSessionCreated:
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
        normalized_recipient is not None
        and normalized_recipient in settings.allowed_demo_emails
    )
    demo = DemoSession(
        id=session_id,
        merchant_id=merchant_id,
        customer_id=customer_id,
        razorpay_order_id=order.id,
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
        session_id=session_id,
        session_token=issue_session_token(session_id, merchant_id, expires_at, secret),
        razorpay_key_id=settings.razorpay_key_id or "rzp_test_simulated",
        razorpay_order_id=order.id,
        amount_paise=demo.amount_paise,
        currency=demo.currency,
        expires_at=expires_at,
        email_mode=(EmailMode.ALLOWLISTED if allowlisted else EmailMode.PREVIEW_ONLY),
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

    demo = session.scalar(
        select(DemoSession).where(DemoSession.id == session_id).with_for_update()
    )
    if demo is None or claims.merchant_id != demo.merchant_id:
        raise DemoSessionUnauthorized("invalid session token")
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
    demo = session.scalar(
        select(DemoSession).where(DemoSession.id == session_id).with_for_update()
    )
    if demo is None:
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
    invalidated = session.scalar(
        select(CheckoutEvent.id)
        .where(
            CheckoutEvent.session_id == session_id,
            CheckoutEvent.received_at > dismissal.received_at,
            CheckoutEvent.event_type.in_(
                [
                    CheckoutEventType.PAYMENT_ATTEMPT_STARTED.value,
                    CheckoutEventType.CHECKOUT_COMPLETED.value,
                ]
            ),
        )
        .limit(1)
    )
    if invalidated is not None:
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
    if existing is not None:
        if existing.leak_type == LeakType.PAYMENT_FAILURE.value:
            demo.state = DemoSessionState.AT_RISK.value
            demo.updated_at = now
            session.commit()
        return existing.id

    started = time.perf_counter()
    try:
        payments = provider.list_order_payments(demo.razorpay_order_id)
    except ProviderError as exc:
        _record_provider_call(
            session,
            session_id=demo.id,
            provider="razorpay",
            operation="list_order_payments",
            request_id=exc.request_id,
            latency_ms=round((time.perf_counter() - started) * 1000),
            status="failed",
            metadata={},
            error_class=exc.error_class,
        )
        session.commit()
        raise
    _record_provider_call(
        session,
        session_id=demo.id,
        provider="razorpay",
        operation="list_order_payments",
        request_id=next((item.request_id for item in payments if item.request_id), None),
        latency_ms=round((time.perf_counter() - started) * 1000),
        status="succeeded",
        metadata={
            "payment_count": len(payments),
            "statuses": sorted({item.status for item in payments}),
        },
    )
    if any(payment.status in {"captured", "authorized"} for payment in payments):
        session.commit()
        return None
    if any(payment.status == "failed" for payment in payments):
        session.commit()
        return None

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
    assert_session_transition(DemoSessionState(demo.state), DemoSessionState.AT_RISK)
    demo.state = DemoSessionState.AT_RISK.value
    demo.updated_at = now
    session.commit()
    return case.id


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
            DemoSession.state.in_(
                [DemoSessionState.CREATED.value, DemoSessionState.CHECKOUT_OPEN.value]
            ),
            DemoSession.expires_at > now,
            ~select(later.id)
            .where(
                later.session_id == dismissal.session_id,
                later.received_at > dismissal.received_at,
                later.event_type.in_(
                    [
                        CheckoutEventType.PAYMENT_ATTEMPT_STARTED.value,
                        CheckoutEventType.CHECKOUT_COMPLETED.value,
                    ]
                ),
            )
            .exists(),
        )
        .order_by(dismissal.received_at, dismissal.id)
        .limit(limit)
    ).all()
    return [(str(session_id), int(event_id)) for session_id, event_id in rows]
