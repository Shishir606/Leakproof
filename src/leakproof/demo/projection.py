from __future__ import annotations

from datetime import UTC, datetime
from statistics import median

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from leakproof.config import Settings
from leakproof.demo.contracts import (
    CaseInsight,
    CaseProjection,
    DemoSessionProjection,
    DemoSessionState,
    EmailMode,
    OperationalMetrics,
    ProviderStatus,
    TimelineItem,
    live_case_dedupe_key,
)
from leakproof.demo.security import (
    InvalidSessionToken,
    SessionTokenExpired,
    verify_session_token,
)
from leakproof.demo.service import DemoSessionExpired, DemoSessionUnauthorized
from leakproof.models.db import (
    CaseInsightRecord,
    CheckoutEvent,
    DemoSession,
    Diagnosis,
    Event,
    LLMCall,
    ProviderCall,
    RecoveryCase,
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _secret(settings: Settings) -> str:
    return settings.recovery_token_secret or "leakproof-simulation-only-signing-secret"


def _safe_event_payload(event: Event) -> dict:
    payload = event.payload or {}
    if event.kind == "DIAGNOSED":
        return {
            key: payload[key]
            for key in ("rule_id", "failure_class", "confidence")
            if key in payload
        }
    if event.kind == "CASE_INSIGHT_READY":
        return {
            key: payload[key]
            for key in ("status", "fallback_reason", "prompt_version", "insight", "cost_paise")
            if key in payload
        }
    allowed = {
        "amount_at_risk",
        "currency",
        "from_leak_type",
        "leak_type",
        "matched_by",
        "outcome",
        "reason",
        "status",
        "to_leak_type",
    }
    return {key: value for key, value in payload.items() if key in allowed}


def _source(event: Event) -> str:
    if event.actor == "luna":
        return "openai"
    if "razorpay" in event.actor:
        return "razorpay"
    return "leakproof"


def get_demo_session_projection(
    session: Session,
    session_id: str,
    *,
    session_token: str,
    settings: Settings,
    now: datetime | None = None,
) -> DemoSessionProjection:
    now = _utc(now or datetime.now(UTC))
    try:
        claims = verify_session_token(session_token, _secret(settings), now=now)
    except SessionTokenExpired as exc:
        raise DemoSessionExpired("demo session has expired") from exc
    except InvalidSessionToken as exc:
        raise DemoSessionUnauthorized("invalid session token") from exc
    demo = session.get(DemoSession, session_id)
    if demo is None or claims.session_id != demo.id or claims.merchant_id != demo.merchant_id:
        raise DemoSessionUnauthorized("invalid session token")
    if _utc(demo.expires_at) <= now and DemoSessionState(demo.state) != DemoSessionState.RECOVERED:
        raise DemoSessionExpired("demo session has expired")

    case = session.scalar(
        select(RecoveryCase)
        .where(
            RecoveryCase.merchant_id == demo.merchant_id,
            or_(
                RecoveryCase.dedupe_key
                == live_case_dedupe_key(demo.id, demo.razorpay_order_id),
                RecoveryCase.customer_id == demo.customer_id,
            ),
        )
        .order_by(RecoveryCase.detected_at.desc())
    )
    diagnosis = session.get(Diagnosis, case.id) if case else None
    insight_record = session.get(CaseInsightRecord, case.id) if case else None
    insight = None
    if insight_record and insight_record.summary:
        insight = CaseInsight(
            summary=insight_record.summary,
            probable_cause=insight_record.probable_cause or "Unavailable",
            evidence=insight_record.evidence,
            recommended_next_step=(
                insight_record.recommended_next_step or "Use deterministic guidance."
            ),
            confidence=float(insight_record.confidence or 0),
        )

    provider_calls = list(
        session.scalars(
            select(ProviderCall)
            .where(
                or_(
                    ProviderCall.session_id == demo.id,
                    ProviderCall.case_id == (case.id if case else "__no_case__"),
                )
            )
            .order_by(ProviderCall.created_at, ProviderCall.id)
        )
    )
    provider_statuses = [
        ProviderStatus(
            provider=call.provider,
            operation=call.operation,
            status=call.status,
            request_id=call.request_id,
        )
        for call in provider_calls
        if call.provider in {"razorpay", "openai", "resend"}
    ]

    timeline: list[TimelineItem] = []
    checkout_events = session.scalars(
        select(CheckoutEvent)
        .where(CheckoutEvent.session_id == demo.id)
        .order_by(CheckoutEvent.received_at, CheckoutEvent.id)
    )
    timeline.extend(
        TimelineItem(
            kind=item.event_type,
            source="browser",
            occurred_at=_utc(item.received_at),
            payload={"metadata": item.event_metadata},
        )
        for item in checkout_events
    )
    if case:
        case_events = session.scalars(
            select(Event).where(Event.case_id == case.id).order_by(Event.seq)
        )
        timeline.extend(
            TimelineItem(
                kind=item.kind,
                source=_source(item),
                occurred_at=_utc(item.occurred_at),
                payload=_safe_event_payload(item),
            )
            for item in case_events
        )
    timeline.extend(
        TimelineItem(
            kind=f"{call.operation}.{call.status}",
            source=call.provider,
            occurred_at=_utc(call.created_at),
            payload={
                "latency_ms": call.latency_ms,
                "attempts": call.attempt_number,
                "error_class": call.error_class,
            },
        )
        for call in provider_calls
        if call.provider in {"razorpay", "openai", "resend"}
    )
    timeline.sort(key=lambda item: item.occurred_at)

    live_cases = list(
        session.scalars(
            select(RecoveryCase).where(
                RecoveryCase.merchant_id == demo.merchant_id,
                RecoveryCase.dedupe_key.like("live:%"),
            )
        )
    )
    recovered = [item for item in live_cases if item.outcome == "RECOVERED"]
    durations = [
        (_utc(item.closed_at) - _utc(item.detected_at)).total_seconds()
        for item in recovered
        if item.closed_at is not None
    ]
    luna_cost = sum(
        row.cost_paise
        for row in session.scalars(
            select(LLMCall)
            .join(RecoveryCase, RecoveryCase.id == LLMCall.case_id)
            .where(
                RecoveryCase.merchant_id == demo.merchant_id,
                RecoveryCase.dedupe_key.like("live:%"),
                LLMCall.purpose == "case_insight",
            )
        )
    )
    return DemoSessionProjection(
        session_id=demo.id,
        state=DemoSessionState(demo.state),
        amount_paise=demo.amount_paise,
        currency=demo.currency,
        expires_at=_utc(demo.expires_at),
        email_mode=(
            EmailMode.ALLOWLISTED if demo.recipient_ciphertext else EmailMode.PREVIEW_ONLY
        ),
        case=(
            CaseProjection(
                case_id=case.id,
                leak_type=case.leak_type,
                state=case.state,
                deterministic_diagnosis=(
                    {
                        "rule_id": diagnosis.rule_id,
                        "failure_class": diagnosis.failure_class,
                        "confidence": float(diagnosis.confidence),
                    }
                    if diagnosis
                    else None
                ),
                insight=insight,
                insight_status=(insight_record.status if insight_record else "pending"),
            )
            if case
            else None
        ),
        recovery_url_available=(
            case is not None
            and DemoSessionState(demo.state)
            in {DemoSessionState.AT_RISK, DemoSessionState.CHECKOUT_OPEN}
        ),
        provider_statuses=provider_statuses,
        timeline=timeline,
        metrics=OperationalMetrics(
            cases_detected=len(live_cases),
            recovered_cases=len(recovered),
            recovered_amount_paise=sum(item.amount_at_risk for item in recovered),
            recovery_rate=(len(recovered) / len(live_cases) if live_cases else 0),
            median_recovery_time_seconds=median(durations) if durations else None,
            provider_failures=sum(call.status == "failed" for call in provider_calls),
            luna_cost_paise=luna_cost,
        ),
    )
