from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leakproof.actuators.executor import _gate_inputs, idempotency_key
from leakproof.audit.timeline import append_event
from leakproof.config import Settings
from leakproof.demo.security import decrypt_recipient
from leakproof.demo.service import issue_demo_recovery_token
from leakproof.guardrails import (
    Gate,
    GateCase,
    GateCustomer,
    GateDiagnosis,
    GatePlan,
    record_gate_verdict,
)
from leakproof.messaging import TemplateRegistry
from leakproof.models.db import (
    Action,
    Contact,
    DemoSession,
    EmailDelivery,
    ProviderCall,
    RecoveryCase,
)
from leakproof.models.domain import Arm, CaseOutcome
from leakproof.providers import EmailProvider, EmailSendRequest, ProviderError
from leakproof.sensors.resend import latest_delivery_status
from leakproof.services import new_id

RECOVERY_EMAIL_TEMPLATE_ID = "util_recovery_email_v1"
RECOVERY_EMAIL_SUBJECT = "Complete your payment securely"


@dataclass(frozen=True)
class EmailExecutionResult:
    action_id: str
    status: str
    provider_email_id: str | None = None
    replayed: bool = False
    quota_warning: bool = False


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _demo_for_case(session: Session, case: RecoveryCase) -> DemoSession | None:
    from leakproof.provider_resources import case_for_session, order_recovery_supported

    for demo in session.scalars(
        select(DemoSession).where(DemoSession.merchant_id == case.merchant_id)
    ):
        if order_recovery_supported(session, demo) or demo.primary_entity_type == "invoice":
            owner = case_for_session(session, demo)
            if owner is not None and owner.id == case.id:
                return demo
    return None


def schedule_demo_recovery_email(
    session: Session,
    case_id: str,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> Action:
    """Persist the live-only email step 30 demo seconds after detection."""
    scheduled_from = _utc(now or datetime.now(UTC))
    case = session.get(RecoveryCase, case_id)
    if case is None:
        raise LookupError(case_id)
    demo = _demo_for_case(session, case)
    if demo is None:
        raise ValueError("live recovery email requires a demo session")
    existing = session.scalar(
        select(Action).where(Action.case_id == case.id, Action.action_type == "email_link")
    )
    if existing is not None:
        return existing
    if case.arm == Arm.HOLDOUT.value:
        raise RuntimeError("holdout cases cannot schedule live recovery email")

    action = Action(
        id=new_id("act"),
        case_id=case.id,
        step_index=1,
        action_type="email_link",
        scheduled_for=scheduled_from + timedelta(seconds=settings.demo_abandonment_delay_seconds),
        idempotency_key=idempotency_key(case.id, 1, 1),
        status="pending",
        attempt_count=0,
        cost_paise=0,
    )
    session.add(action)
    append_event(
        session,
        case,
        kind="PLANNED",
        payload={
            "plan": {
                "mode": "live_demo",
                "ladder_id": "live_checkout_recovery",
                "max_steps": 2,
                "steps": [
                    {"step_index": 0, "action_type": "recovery_link", "delay_seconds": 0},
                    {
                        "step_index": 1,
                        "action_type": "email_link",
                        "delay_seconds": settings.demo_abandonment_delay_seconds,
                    },
                ],
            }
        },
        actor="live_demo_planner",
    )
    session.commit()
    return action


def _preview(
    session: Session,
    action: Action,
    demo: DemoSession,
    *,
    status: str,
    now: datetime,
) -> EmailExecutionResult:
    delivery = EmailDelivery(
        session_id=demo.id,
        case_id=action.case_id,
        action_id=action.id,
        recipient_hash=demo.recipient_hash
        or hashlib.sha256(f"no-recipient:{demo.id}".encode()).hexdigest(),
        status=status,
        created_at=now,
        updated_at=now,
    )
    session.add(delivery)
    action.status = status
    action.executed_at = now
    action.attempt_count += 1
    append_event(
        session,
        session.get(RecoveryCase, action.case_id),
        kind="ACTED",
        payload={
            "action_type": "email_link",
            "provider": "resend",
            "status": status,
        },
        actor="resend_actuator",
    )
    session.commit()
    return EmailExecutionResult(action.id, status)


def _usage_counts(session: Session, now: datetime) -> tuple[int, int]:
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    actual = EmailDelivery.provider_email_id.is_not(None)
    daily = int(
        session.scalar(
            select(func.count(EmailDelivery.id)).where(
                actual, EmailDelivery.created_at >= day_start
            )
        )
        or 0
    )
    monthly = int(
        session.scalar(
            select(func.count(EmailDelivery.id)).where(
                actual, EmailDelivery.created_at >= month_start
            )
        )
        or 0
    )
    return daily, monthly


def execute_demo_recovery_email(
    session: Session,
    action_id: str,
    *,
    provider: EmailProvider,
    settings: Settings,
    now: datetime | None = None,
    invoice_provider=None,
) -> EmailExecutionResult:
    """Execute a live email once, degrading every policy/budget block to a safe preview."""
    attempted_at = _utc(now or datetime.now(UTC))
    candidate = session.get(Action, action_id)
    candidate_case = session.get(RecoveryCase, candidate.case_id) if candidate else None
    candidate_demo = _demo_for_case(session, candidate_case) if candidate_case else None
    if candidate_demo and candidate_demo.primary_entity_type == "invoice":
        from leakproof.demo.invoices import invoice_scope
        from leakproof.provider_resources import _lock_scope

        _lock_scope(session, invoice_scope(candidate_demo))
    action = session.scalar(
        select(Action)
        .where(Action.id == action_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if action is None:
        raise LookupError(action_id)
    previous = session.scalar(select(EmailDelivery).where(EmailDelivery.case_id == action.case_id))
    if previous is not None:
        return EmailExecutionResult(
            action.id,
            previous.status,
            previous.provider_email_id,
            replayed=True,
        )
    if _utc(action.scheduled_for) > attempted_at:
        return EmailExecutionResult(action.id, "not_due")

    case = session.get(RecoveryCase, action.case_id)
    if case is None:
        raise ValueError("email action has no case")
    demo = _demo_for_case(session, case)
    if demo is None:
        raise ValueError("email action has no demo session")
    if case.outcome == CaseOutcome.RECOVERED.value or action.status == "cancelled":
        action.status = "cancelled"
        session.commit()
        return EmailExecutionResult(action.id, "cancelled")

    if _utc(demo.expires_at) <= attempted_at or demo.state == "EXPIRED":
        action.status = "cancelled"
        demo.state = "EXPIRED"
        demo.updated_at = attempted_at
        append_event(
            session,
            case,
            kind="ACTED",
            actor="live_demo_planner",
            payload={
                "action_type": "email_link",
                "status": "cancelled",
                "reason": "session_expired",
            },
        )
        session.commit()
        return EmailExecutionResult(action.id, "cancelled")

    invoice = None
    if demo.primary_entity_type == "invoice":
        from leakproof.demo.invoices import invoice_view, reconcile_invoice
        from leakproof.providers.factory import get_payment_provider

        try:
            reconcile_invoice(
                session,
                demo.id,
                provider=invoice_provider or get_payment_provider(),
                settings=settings,
                now=attempted_at,
                commit=False,
            )
        except ProviderError:
            return EmailExecutionResult(action.id, "provider_retry")
        invoice = invoice_view(session, demo, attempted_at)
        if invoice["disposition"] != "payable" or case.outcome == "RECOVERED":
            action.status = "cancelled"
            session.commit()
            return EmailExecutionResult(action.id, "cancelled")

    if not settings.outbound_email_enabled:
        return _preview(session, action, demo, status="preview_only", now=attempted_at)

    # No recipient data is decrypted for preview-only sessions.
    if not demo.recipient_ciphertext or not demo.recipient_hash:
        return _preview(session, action, demo, status="preview_only", now=attempted_at)

    recipient = decrypt_recipient(demo.recipient_ciphertext, settings.recovery_token_secret)
    if recipient.casefold() not in settings.allowed_demo_emails:
        return _preview(session, action, demo, status="preview_only", now=attempted_at)

    rolling_day = attempted_at - timedelta(days=1)
    recipient_sends = int(
        session.scalar(
            select(func.count(EmailDelivery.id)).where(
                EmailDelivery.recipient_hash == demo.recipient_hash,
                EmailDelivery.provider_email_id.is_not(None),
                EmailDelivery.created_at >= rolling_day,
            )
        )
        or 0
    )
    if recipient_sends >= 5:
        return _preview(session, action, demo, status="rate_limited", now=attempted_at)

    daily, monthly = _usage_counts(session, attempted_at)
    if daily >= settings.resend_daily_limit or monthly >= settings.resend_monthly_limit:
        return _preview(session, action, demo, status="quota_blocked", now=attempted_at)
    if not settings.resend_api_key or not settings.resend_from_email:
        return _preview(session, action, demo, status="preview_only", now=attempted_at)

    planned, context = _gate_inputs(session, action, case, attempted_at=attempted_at)
    verdict = Gate().evaluate(
        GateCase(
            merchant_id=case.merchant_id,
            attempts=context["attempts"],
            retries=context["retries"],
        ),
        planned,
        customer=GateCustomer(
            dnc=context["customer"].dnc,
            protected=context["customer"].protected,
        ),
        diagnosis=GateDiagnosis(failure_class=context["diagnosis"].failure_class),
        plan=GatePlan(max_steps=context["max_steps"]),
        contacts=context["contacts"],
        suppression_matches=context["suppression_matches"],
        merchant_policy=context["merchant"].policy or {},
    )
    record_gate_verdict(session, case, action, verdict)
    if verdict.decision == "RESCHEDULE":
        action.scheduled_for = verdict.retry_at
        action.status = "pending"
        session.commit()
        return EmailExecutionResult(action.id, "rescheduled")
    if verdict.decision != "ALLOW":
        return _preview(
            session,
            action,
            demo,
            status="denied" if verdict.decision == "DENY" else "deferred",
            now=attempted_at,
        )

    token = issue_demo_recovery_token(session, demo.id, settings=settings, now=attempted_at)
    recovery_url = f"{settings.public_base_url.rstrip('/')}/recover/{quote(token, safe='')}"
    template_id = "util_invoice_recovery_email_v1" if invoice else RECOVERY_EMAIL_TEMPLATE_ID
    amount = invoice["outstanding_balance_paise"] if invoice else case.amount_at_risk
    message = TemplateRegistry().render(
        template_id,
        {
            "customer_ref": "there",
            "amount": f"INR {amount / 100:,.2f}",
            "link": recovery_url,
        },
        language="en-IN",
    )
    key = action.idempotency_key or idempotency_key(case.id, action.step_index, 1)
    action.idempotency_key = key
    try:
        result = provider.send_recovery_email(
            EmailSendRequest(
                action_id=action.id,
                case_id=case.id,
                recipient=recipient,
                template_id=template_id,
                template_variables={
                    "subject": "Your invoice has an outstanding balance"
                    if invoice
                    else RECOVERY_EMAIL_SUBJECT,
                    "body": message.body,
                    "recovery_url": recovery_url,
                },
                idempotency_key=key,
            )
        )
    except ProviderError as exc:
        session.add(
            ProviderCall(
                session_id=demo.id,
                case_id=case.id,
                action_id=action.id,
                provider="resend",
                operation="send_recovery_email",
                request_id=exc.request_id,
                safe_response_metadata={},
                latency_ms=exc.latency_ms,
                attempt_number=exc.attempts,
                status="failed",
                error_class=exc.error_class,
            )
        )
        action.status = "failed"
        action.attempt_count += exc.attempts
        action.executed_at = attempted_at
        session.commit()
        return EmailExecutionResult(action.id, "failed")

    warning = (daily + 1) / settings.resend_daily_limit >= 0.8 or (
        monthly + 1
    ) / settings.resend_monthly_limit >= 0.8
    delivery = EmailDelivery(
        session_id=demo.id,
        case_id=case.id,
        action_id=action.id,
        provider_email_id=result.provider_email_id,
        recipient_hash=demo.recipient_hash,
        status=result.status,
        created_at=attempted_at,
        updated_at=attempted_at,
    )
    session.add(delivery)
    session.flush()
    delivery.status = latest_delivery_status(
        session, result.provider_email_id, default=result.status
    )
    session.add(
        ProviderCall(
            session_id=demo.id,
            case_id=case.id,
            action_id=action.id,
            provider="resend",
            operation="send_recovery_email",
            request_id=result.request_id,
            safe_response_metadata={
                "provider_email_id": result.provider_email_id,
                "quota_warning": warning,
            },
            latency_ms=result.latency_ms,
            attempt_number=result.attempts,
            status="succeeded",
        )
    )
    action.status = "succeeded"
    action.provider_ref = result.provider_email_id
    action.executed_at = attempted_at
    action.attempt_count += result.attempts
    session.add(
        Contact(
            customer_id=case.customer_id,
            channel="email",
            case_id=case.id,
            sent_at=attempted_at,
        )
    )
    append_event(
        session,
        case,
        kind="ACTED",
        payload={
            "action_type": "email_link",
            "provider": "resend",
            "status": "sent",
            "quota_warning": warning,
        },
        actor="resend_actuator",
    )
    session.commit()
    return EmailExecutionResult(
        action.id,
        delivery.status,
        result.provider_email_id,
        quota_warning=warning,
    )
