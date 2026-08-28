from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from leakproof.actuators.base import ActuatorRequest, ActuatorResult
from leakproof.actuators.simulator import SimulatorActuatorRegistry
from leakproof.audit.timeline import append_event
from leakproof.config import get_policy_config, get_settings
from leakproof.diagnosis.tier2 import case_matches_open_suppression
from leakproof.guardrails import (
    ContactRecord,
    Gate,
    GateCase,
    GateCustomer,
    GateDiagnosis,
    GatePlan,
    PlannedAction,
    record_gate_verdict,
)
from leakproof.messaging import RenderedMessage, TemplateRegistry
from leakproof.models.db import (
    Action,
    Consent,
    Contact,
    Customer,
    Diagnosis,
    Event,
    Merchant,
    RecoveryCase,
)
from leakproof.models.domain import Arm, CaseOutcome, LeakType
from leakproof.services import attribution_window


@dataclass(frozen=True)
class ExecutionResult:
    action_id: str
    status: str
    provider_ref: str | None = None
    replayed: bool = False


def idempotency_key(case_id: str, step_index: int, attempt: int) -> str:
    raw = f"{case_id}:{step_index}:{attempt}".encode()
    return f"lp_{hashlib.sha256(raw).hexdigest()}"


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _plan_payload(session: Session, case_id: str) -> dict:
    event = session.scalar(
        select(Event)
        .where(Event.case_id == case_id, Event.kind == "PLANNED")
        .order_by(Event.seq.desc())
    )
    if event is None:
        raise ValueError(f"case {case_id} has no persisted plan")
    return dict(event.payload["plan"])


def _registered_message(
    action: Action,
    case: RecoveryCase,
    customer: Customer,
) -> RenderedMessage | None:
    template_by_action = {
        "alt_method_prompt": "util_recovery_in_app_v1",
        "email_link": "util_recovery_email_v1",
        "whatsapp_link": "util_recovery_whatsapp_v1",
        "sms_link": "util_recovery_sms_v1",
        "voice_hinglish": "util_recovery_voice_v1",
    }
    template_id = template_by_action.get(action.action_type)
    if template_id is None:
        return None
    variables = (
        {}
        if action.action_type == "voice_hinglish"
        else {
            "customer_ref": customer.id,
            "amount": f"INR {case.amount_at_risk / 100:,.2f}",
            "link": f"https://pay.example/{case.entity_id}",
        }
    )
    return TemplateRegistry().render(
        template_id,
        variables,
        language="hinglish" if action.action_type == "voice_hinglish" else customer.locale,
    )


def _gate_inputs(
    session: Session,
    action: Action,
    case: RecoveryCase,
    *,
    attempted_at: datetime,
) -> tuple[PlannedAction, dict]:
    customer = session.get(Customer, case.customer_id)
    merchant = session.get(Merchant, case.merchant_id)
    diagnosis = session.get(Diagnosis, case.id)
    if customer is None or merchant is None or diagnosis is None:
        raise ValueError("action execution requires case principals and diagnosis")
    config = next(
        item for item in get_policy_config().actions if item.key == action.action_type
    )
    consent = (
        session.scalar(
            select(Consent).where(
                Consent.customer_id == customer.id,
                Consent.channel == config.channel,
                Consent.granted.is_(True),
            )
        )
        if config.channel
        else None
    )
    prior_actions = list(
        session.scalars(
            select(Action).where(
                Action.case_id == case.id,
                Action.status == "succeeded",
                Action.step_index < action.step_index,
            )
        )
    )
    last_retry = max(
        (
            _aware(item.executed_at)
            for item in prior_actions
            if item.action_type == "silent_retry" and item.executed_at is not None
        ),
        default=None,
    )
    message = _registered_message(action, case, customer) if config.customer_facing else None
    planned = PlannedAction(
        action_type=action.action_type,
        scheduled_for=attempted_at,
        is_customer_facing=config.customer_facing,
        channel=config.channel,
        amount_paise=case.amount_at_risk,
        consent_granted=consent is not None,
        consent_basis=consent.basis if consent is not None else None,
        rendered_message=message,
        last_retry_at=last_retry,
        makes_debit=action.action_type == "silent_retry",
        mandate_max_amount_paise=case.amount_at_risk,
        invoice_outstanding_paise=case.amount_at_risk,
        standing_merchant_approval=bool(
            (merchant.policy or {}).get("standing_merchant_approval", False)
        ),
    )
    contacts = [
        ContactRecord(channel=item.channel, sent_at=_aware(item.sent_at))
        for item in session.scalars(
            select(Contact).where(Contact.customer_id == customer.id)
        )
    ]
    plan = _plan_payload(session, case.id)
    context = {
        "customer": customer,
        "merchant": merchant,
        "diagnosis": diagnosis,
        "config": config,
        "message": message,
        "contacts": contacts,
        "max_steps": int(plan["max_steps"]),
        "attempts": len(prior_actions),
        "retries": sum(item.action_type == "silent_retry" for item in prior_actions),
        "suppression_matches": case_matches_open_suppression(
            session, case, now=attempted_at
        ),
    }
    return planned, context


def execute_action(
    session: Session,
    action_id: str,
    *,
    now: datetime | None = None,
    registry: SimulatorActuatorRegistry | None = None,
) -> ExecutionResult:
    """Gate and execute one scheduled action safely under Celery redelivery."""
    attempted_at = _aware(now or datetime.now(UTC))
    action = session.scalar(
        select(Action).where(Action.id == action_id).with_for_update()
    )
    if action is None:
        raise LookupError(action_id)
    if action.status == "succeeded":
        return ExecutionResult(action.id, "succeeded", action.provider_ref, replayed=True)
    if action.status in {"cancelled", "denied", "deferred"}:
        return ExecutionResult(action.id, str(action.status), action.provider_ref, replayed=True)
    if _aware(action.scheduled_for) > attempted_at:
        return ExecutionResult(action.id, "not_due")

    case = session.get(RecoveryCase, action.case_id)
    if case is None:
        raise ValueError(f"action {action.id} has no case")
    if case.arm == Arm.HOLDOUT.value:
        raise RuntimeError("holdout cases cannot execute interventions")
    if case.outcome == CaseOutcome.RECOVERED.value:
        action.status = "cancelled"
        session.commit()
        return ExecutionResult(action.id, "cancelled")

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
        assert verdict.retry_at is not None
        action.scheduled_for = verdict.retry_at
        action.status = "pending"
        session.commit()
        return ExecutionResult(action.id, "rescheduled")
    if verdict.decision == "DENY":
        action.status = "denied"
        session.commit()
        return ExecutionResult(action.id, "denied")
    if verdict.decision == "DEFER_TO_HUMAN":
        action.status = "deferred"
        append_event(
            session,
            case,
            kind="ESCALATED",
            payload={"action_id": action.id, "reason": verdict.reason},
            actor="dispatcher",
        )
        session.commit()
        return ExecutionResult(action.id, "deferred")

    attempt = action.attempt_count + 1
    key = action.idempotency_key or idempotency_key(case.id, action.step_index, attempt)
    action.idempotency_key = key
    request = ActuatorRequest(
        action_id=action.id,
        action_type=action.action_type,
        case_id=case.id,
        entity_id=case.entity_id,
        customer_id=case.customer_id,
        amount_paise=case.amount_at_risk,
        currency=case.currency,
        idempotency_key=key,
        channel=context["config"].channel,
        message=context["message"],
    )
    if get_settings().mode != "simulation" and registry is None:
        raise RuntimeError("live actuators are scheduled for the August 30 integration slice")
    result: ActuatorResult = (registry or SimulatorActuatorRegistry()).for_action(
        action.action_type
    ).execute(session, request, verdict)
    action.attempt_count = attempt
    action.provider_ref = result.provider_ref
    action.executed_at = attempted_at
    action.status = result.status
    if result.status == "succeeded":
        case.attribution_until = attempted_at + attribution_window(LeakType(case.leak_type))
    if context["config"].customer_facing:
        session.add(
            Contact(
                customer_id=case.customer_id,
                channel=context["config"].channel,
                case_id=case.id,
                sent_at=attempted_at,
            )
        )
    append_event(
        session,
        case,
        kind="ACTED",
        payload={
            "action_id": action.id,
            "action_type": action.action_type,
            "idempotency_key": key,
            "provider": result.provider,
            "provider_ref": result.provider_ref,
            "status": result.status,
            "simulated": result.response.get("simulated", False),
        },
        actor=f"{result.provider}_actuator",
    )
    session.commit()
    return ExecutionResult(action.id, result.status, result.provider_ref, result.replayed)


def due_action_ids(session: Session, *, now: datetime | None = None, limit: int = 100) -> list[str]:
    due_at = _aware(now or datetime.now(UTC))
    return list(
        session.scalars(
            select(Action.id)
            .join(RecoveryCase, RecoveryCase.id == Action.case_id)
            .where(
                Action.status == "pending",
                Action.scheduled_for <= due_at,
                RecoveryCase.outcome.is_(None),
            )
            .order_by(Action.scheduled_for, Action.id)
            .limit(limit)
        )
    )
