from __future__ import annotations

from datetime import UTC, datetime, timedelta
from statistics import median

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from leakproof.config import Settings
from leakproof.demo.contracts import (
    AbandonmentCheck,
    CaseInsight,
    CaseProjection,
    DemoSessionProjection,
    DemoSessionState,
    EmailMode,
    OperationalMetrics,
    ProviderStatus,
    RecoveryActionProjection,
    TimelineItem,
)
from leakproof.demo.security import (
    InvalidSessionToken,
    SessionTokenExpired,
    verify_session_token,
)
from leakproof.demo.service import (
    DemoSessionExpired,
    DemoSessionUnauthorized,
    issue_demo_recovery_token,
    latest_checkout_event,
)
from leakproof.models.db import (
    Action,
    CaseInsightRecord,
    CheckoutEvent,
    DemoSession,
    Diagnosis,
    EmailDelivery,
    EmailDeliveryEvent,
    Event,
    LLMCall,
    ProviderCall,
    ProviderEntity,
    ProviderObligation,
    RecoveryAttribution,
    RecoveryCase,
)
from leakproof.provenance import DataProvenance


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
    if event.kind == "PLANNED":
        plan = payload.get("plan") or {}
        return {
            "mode": plan.get("mode"),
            "ladder_id": plan.get("ladder_id"),
            "steps": [
                {
                    key: step[key]
                    for key in ("step_index", "action_type", "delay_seconds")
                    if key in step
                }
                for step in plan.get("steps", [])[:4]
                if isinstance(step, dict)
            ],
        }
    if event.kind == "GATE":
        return {key: payload[key] for key in ("decision", "reason", "retry_at") if key in payload}
    if event.kind == "ACTED":
        return {
            key: payload[key]
            for key in ("action_type", "provider", "status", "quota_warning")
            if key in payload
        }
    if event.kind in {
        "AI_PROPOSED",
        "AI_PROPOSAL_REJECTED",
        "POLICY_VALIDATED",
        "SUPPRESSION_OPENED",
        "RETRY_DELAYED",
        "MERCHANT_ALERTED",
        "NO_ACTION",
        "AI_DEGRADED",
    }:
        return {
            key: payload[key]
            for key in (
                "pattern",
                "scope",
                "evidence",
                "evidence_slice_ids",
                "confidence",
                "recommended_action",
                "ttl_minutes",
                "reason",
                "consequence",
                "actions_delayed",
            )
            if key in payload
        }
    allowed = {
        "case_open",
        "provider_status",
        "business_overdue",
        "amount_due_paise",
        "amount_paid_paise",
        "disposition",
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
    evidence_source = (event.payload or {}).get("evidence", {}).get("source")
    if evidence_source == "browser_telemetry":
        return "browser"
    if evidence_source == "razorpay_webhook":
        return "razorpay"
    if evidence_source == "razorpay_checkout_signature_and_api":
        return "razorpay"
    if evidence_source == "razorpay_payment_api":
        return "razorpay"
    if event.actor == "luna" or "openai" in event.actor:
        return "openai"
    if "razorpay" in event.actor:
        return "razorpay"
    if "resend" in event.actor:
        return "resend"
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

    from leakproof.demo.invoices import invoice_view
    from leakproof.demo.subscriptions import subscription_view
    from leakproof.provider_resources import case_for_session, order_recovery_supported

    invoice = invoice_view(session, demo, now)
    subscription = subscription_view(session, demo, now)
    case = case_for_session(session, demo)
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
            latency_ms=call.latency_ms,
            attempts=call.attempt_number,
            error_class=call.error_class,
        )
        for call in provider_calls
        if call.provider in {"razorpay", "openai", "resend"}
    ]
    email_delivery = (
        session.scalar(select(EmailDelivery).where(EmailDelivery.case_id == case.id))
        if case
        else None
    )
    if email_delivery is not None:
        provider_statuses.append(
            ProviderStatus(
                provider="resend",
                operation="delivery",
                status=email_delivery.status,
                request_id=email_delivery.provider_email_id,
            )
        )

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
    if email_delivery is not None:
        delivery_events = (
            list(
                session.scalars(
                    select(EmailDeliveryEvent)
                    .where(EmailDeliveryEvent.provider_email_id == email_delivery.provider_email_id)
                    .order_by(EmailDeliveryEvent.occurred_at, EmailDeliveryEvent.id)
                )
            )
            if email_delivery.provider_email_id
            else []
        )
        if delivery_events:
            timeline.extend(
                TimelineItem(
                    kind=item.event_type,
                    source="resend",
                    occurred_at=_utc(item.occurred_at),
                    payload={"status": (item.safe_payload or {}).get("status")},
                )
                for item in delivery_events
            )
        else:
            timeline.append(
                TimelineItem(
                    kind="email.delivery",
                    source="resend",
                    occurred_at=_utc(email_delivery.updated_at),
                    payload={"status": email_delivery.status},
                )
            )
    timeline.sort(key=lambda item: item.occurred_at)

    actions = (
        list(
            session.scalars(
                select(Action)
                .where(Action.case_id == case.id)
                .order_by(Action.step_index, Action.scheduled_for)
            )
        )
        if case
        else []
    )
    recovery_url_available = (
        (
            order_recovery_supported(session, demo)
            or invoice is not None
            and invoice["disposition"] == "payable"
            or subscription is not None
            and subscription["method_update_available"]
        )
        and case is not None
    ) and DemoSessionState(demo.state) in {
        DemoSessionState.AT_RISK,
        DemoSessionState.CHECKOUT_OPEN,
    }
    recovery_path = None
    if recovery_url_available:
        recovery_token = issue_demo_recovery_token(
            session,
            demo.id,
            settings=settings,
            now=now,
        )
        # The token is returned only to the authenticated session projection. It is never
        # persisted in provider metadata, event payloads, logs, or acceptance exports.
        recovery_path = f"/recover/{recovery_token}"
    recovery_actions: list[RecoveryActionProjection] = []
    if case is not None:
        recovery_actions.append(
            RecoveryActionProjection(
                action_type=(
                    "merchant_review"
                    if invoice
                    and invoice["disposition"] == "merchant_review"
                    or subscription
                    and not subscription["method_update_available"]
                    else "invoice_payment_link"
                    if invoice
                    else "subscription_method_update"
                    if subscription
                    else "recovery_link"
                ),
                status=(
                    "available"
                    if recovery_url_available
                    else "completed"
                    if DemoSessionState(demo.state) == DemoSessionState.RECOVERED
                    else "unavailable"
                ),
                scheduled_for=_utc(case.detected_at),
                executed_at=_utc(case.detected_at),
                gate_verdict="NOT_REQUIRED",
            )
        )
        recovery_actions.extend(
            RecoveryActionProjection(
                action_id=action.id,
                action_type="email_link",
                status=action.status or "pending",
                scheduled_for=_utc(action.scheduled_for),
                executed_at=_utc(action.executed_at) if action.executed_at else None,
                gate_verdict=action.verdict,
                provider_receipt_id=action.provider_ref,
            )
            for action in actions
            if action.action_type == "email_link"
        )

    registered_cases = (
        select(ProviderObligation.case_id)
        .join(ProviderEntity, ProviderEntity.obligation_id == ProviderObligation.id)
        .where(
            ProviderObligation.merchant_id == demo.merchant_id,
            ProviderObligation.mode == demo.provider_mode,
            ProviderEntity.session_id.is_not(None),
            ProviderObligation.reconciliation_required.is_(False),
        )
    )
    live_cases = list(
        session.scalars(
            select(RecoveryCase).where(
                RecoveryCase.merchant_id == demo.merchant_id,
                or_(RecoveryCase.dedupe_key.like("live:%"), RecoveryCase.id.in_(registered_cases)),
                ~select(ProviderObligation.id)
                .where(
                    ProviderObligation.case_id == RecoveryCase.id,
                    ProviderObligation.reconciliation_required.is_(True),
                )
                .exists(),
            )
        )
    )
    measured_ids = {item.id for item in live_cases} | ({case.id} if case else set())
    credited_amounts = {
        row.case_id: row.amount_paise
        for row in session.scalars(
            select(RecoveryAttribution).where(RecoveryAttribution.case_id.in_(measured_ids))
        )
    }
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
    live_case_ids = [item.id for item in live_cases]
    live_session_ids = list(
        session.scalars(select(DemoSession.id).where(DemoSession.merchant_id == demo.merchant_id))
    )
    live_provider_failures = 0
    if live_case_ids or live_session_ids:
        live_provider_failures = sum(
            call.status == "failed"
            for call in session.scalars(
                select(ProviderCall).where(
                    or_(
                        ProviderCall.case_id.in_(live_case_ids),
                        ProviderCall.session_id.in_(live_session_ids),
                    )
                )
            )
        )
    end_at = _utc(case.closed_at) if case and case.closed_at else now
    end_to_end_latency = (
        max(0.0, (end_at - _utc(case.detected_at)).total_seconds()) if case else None
    )
    latest_gate = next(
        (action.verdict for action in reversed(actions) if action.verdict is not None),
        None,
    )
    latest = latest_checkout_event(session, demo.id)
    unpaid_confirmed = any(
        call.operation == "list_order_payments"
        and call.status == "succeeded"
        and call.safe_response_metadata.get("unpaid_confirmed") is True
        for call in provider_calls
    )
    abandonment = AbandonmentCheck(unpaid_confirmed=unpaid_confirmed)
    if demo.state == "RECOVERED":
        abandonment.status = "recovered"
    elif case and case.leak_type == "PAYMENT_FAILURE":
        abandonment.status = "payment_failure"
    elif latest and latest.event_type == "checkout_dismissed":
        abandonment.browser_dismissed_at = _utc(latest.received_at)
        abandonment.due_at = _utc(latest.received_at) + timedelta(
            seconds=settings.demo_abandonment_delay_seconds
        )
        check = next(
            (
                call
                for call in reversed(provider_calls)
                if call.operation == "list_order_payments"
                and call.safe_response_metadata.get("dismissal_event_id") == latest.id
            ),
            None,
        )
        if check and check.status == "failed":
            abandonment.status = "provider_retry"
        elif check and not check.safe_response_metadata.get("check_resolved"):
            abandonment.status = "provider_pending"
        elif check and case:
            abandonment.status = "confirmed"
        else:
            abandonment.status = "waiting" if now < abandonment.due_at else "provider_recheck"
    elif case and case.leak_type == "CHECKOUT_ABANDON":
        abandonment.status = "confirmed"
    if abandonment.status == "provider_pending":
        recovery_url_available = False
        recovery_path = None
        for action in recovery_actions:
            if action.action_type == "recovery_link":
                action.status = "unavailable"
    provenance = (
        DataProvenance.LIVE_PROVIDER_VERIFIED
        if settings.mode == "live_demo" and demo.primary_entity_type == "order"
        else DataProvenance(demo.capability_evidence)
    )
    if (
        settings.mode == "live_demo"
        and case
        and case.leak_type == "CHECKOUT_ABANDON"
        and unpaid_confirmed
    ):
        provenance = DataProvenance.LIVE_TELEMETRY_PROVIDER_RECONCILED
    return DemoSessionProjection(
        invoice=invoice,
        subscription=subscription,
        abandonment_check=abandonment,
        scenario_type=demo.scenario_type,
        primary_entity_type=demo.primary_entity_type,
        setup_state=demo.setup_state,
        capability_evidence=demo.capability_evidence,
        data_provenance=provenance,
        session_id=demo.id,
        state=DemoSessionState(demo.state),
        amount_paise=demo.amount_paise,
        currency=demo.currency,
        expires_at=_utc(demo.expires_at),
        email_mode=(EmailMode.ALLOWLISTED if demo.recipient_ciphertext else EmailMode.PREVIEW_ONLY),
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
        recovery_url_available=recovery_url_available,
        recovery_path=recovery_path,
        gate_verdict=latest_gate,
        recovery_actions=recovery_actions,
        provider_statuses=provider_statuses,
        timeline=timeline,
        end_to_end_latency_seconds=end_to_end_latency,
        metrics=OperationalMetrics(
            cases_detected=int(case is not None),
            recovered_cases=int(
                case is not None and DemoSessionState(demo.state) == DemoSessionState.RECOVERED
            ),
            recovered_amount_paise=credited_amounts.get(case.id, 0) if case else 0,
            recovery_rate=(
                1.0
                if case is not None and DemoSessionState(demo.state) == DemoSessionState.RECOVERED
                else 0.0
            ),
            median_recovery_time_seconds=(
                end_to_end_latency
                if case is not None and DemoSessionState(demo.state) == DemoSessionState.RECOVERED
                else None
            ),
            provider_failures=sum(call.status == "failed" for call in provider_calls),
            luna_cost_paise=sum(
                row.cost_paise
                for row in session.scalars(
                    select(LLMCall).where(
                        LLMCall.case_id == (case.id if case else "__no_case__"),
                        LLMCall.purpose == "case_insight",
                    )
                )
            ),
        ),
        environment_metrics=OperationalMetrics(
            cases_detected=len(live_cases),
            recovered_cases=len(recovered),
            recovered_amount_paise=sum(credited_amounts.get(item.id, 0) for item in live_cases),
            recovery_rate=(len(recovered) / len(live_cases) if live_cases else 0),
            median_recovery_time_seconds=median(durations) if durations else None,
            provider_failures=live_provider_failures,
            luna_cost_paise=luna_cost,
        ),
    )
