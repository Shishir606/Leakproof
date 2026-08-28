from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from leakproof.models.db import Customer, Diagnosis, Event, RecoveryCase
from leakproof.models.domain import CaseOutcome, CaseState


class ExceptionItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    reason: str
    detail: str
    leak_type: str
    state: str
    outcome: str | None
    amount_at_risk_paise: int


class ExceptionGroup(BaseModel):
    model_config = ConfigDict(frozen=True)

    reason: str
    detail: str
    cases: int
    amount_at_risk_paise: int


class ExceptionReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    total_cases: int
    total_amount_at_risk_paise: int
    groups: list[ExceptionGroup]
    items: list[ExceptionItem]


def _reason(
    case: RecoveryCase,
    customer: Customer | None,
    diagnosis: Diagnosis | None,
    events: list[Event],
) -> tuple[str, str]:
    if case.outcome == CaseOutcome.SUPPRESSED.value:
        return "COHORT_SUPPRESSION", "Scoped incident circuit breaker prevented an unsafe chase."
    if case.outcome == CaseOutcome.HUMAN.value or case.state == CaseState.ESCALATED.value:
        return (
            "HUMAN_REVIEW",
            "A protected, disputed, high-value, or sensitive case needs a second key.",
        )
    if case.state == CaseState.STOPPED.value:
        return "CUSTOMER_OPT_OUT", "Customer opted out; all later contact was cancelled."
    if customer is not None and customer.dnc:
        return "CONTACT_PROHIBITED", "Recorded do-not-contact status prevents intervention."
    if customer is not None and customer.protected:
        return "PROTECTED_CUSTOMER", "Protected customer policy permits human handling only."
    if diagnosis is not None and diagnosis.failure_class == "MERCHANT_FAULT":
        return "MERCHANT_REMEDIATION", "Merchant configuration must be corrected before retrying."
    if case.outcome == CaseOutcome.ABANDONED.value:
        return (
            "NO_POSITIVE_EV_PATH",
            "No guardrail-eligible action in the bounded ladder had positive EV.",
        )
    if case.outcome == CaseOutcome.LOST.value:
        return (
            "RECOVERY_NOT_OBSERVED",
            "No verified recovery arrived inside the declared attribution window.",
        )
    last = events[-1].kind if events else case.state
    return "WORKFLOW_INCOMPLETE", f"The workflow remains open after {last}."


def exception_report(session: Session, run_id: str) -> ExceptionReport:
    cases = list(
        session.scalars(
            select(RecoveryCase)
            .where(
                RecoveryCase.batch_run_id == run_id,
                or_(
                    RecoveryCase.outcome.is_(None),
                    RecoveryCase.outcome != CaseOutcome.RECOVERED.value,
                ),
            )
            .order_by(RecoveryCase.leak_type, RecoveryCase.id)
        )
    )
    items: list[ExceptionItem] = []
    for case in cases:
        customer = session.get(Customer, case.customer_id)
        diagnosis = session.get(Diagnosis, case.id)
        events = list(
            session.scalars(select(Event).where(Event.case_id == case.id).order_by(Event.seq))
        )
        reason, detail = _reason(case, customer, diagnosis, events)
        items.append(
            ExceptionItem(
                case_id=case.id,
                reason=reason,
                detail=detail,
                leak_type=case.leak_type,
                state=case.state,
                outcome=case.outcome,
                amount_at_risk_paise=case.amount_at_risk,
            )
        )

    grouped: dict[tuple[str, str], list[ExceptionItem]] = defaultdict(list)
    for item in items:
        grouped[(item.reason, item.detail)].append(item)
    groups = [
        ExceptionGroup(
            reason=reason,
            detail=detail,
            cases=len(group),
            amount_at_risk_paise=sum(item.amount_at_risk_paise for item in group),
        )
        for (reason, detail), group in grouped.items()
    ]
    groups.sort(key=lambda item: (-item.cases, item.reason))
    return ExceptionReport(
        run_id=run_id,
        total_cases=len(items),
        total_amount_at_risk_paise=sum(item.amount_at_risk_paise for item in items),
        groups=groups,
        items=items,
    )
