from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leakproof.models.db import (
    Action,
    BatchRun,
    Contact,
    Customer,
    LLMCall,
    Merchant,
    RecoveryAttribution,
    RecoveryCase,
)
from leakproof.models.domain import Arm, CaseOutcome, CaseState


class ArmMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    eligible_cases: int
    recovered_cases: int
    amount_at_risk_paise: int
    recovered_paise: int
    recovery_rate: float


class Scoreboard(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    merchant_id: str
    synthetic: bool
    started_at: datetime
    completed_at: datetime
    duration_seconds: int
    cases_processed: int
    cases_by_leak_type: dict[str, int]
    throughput_cases_per_minute: float
    treatment: ArmMetrics
    holdout: ArmMetrics
    lift_percentage_points: float
    gross_recovered_paise: int
    organic_holdout_paise: int
    counterfactual_organic_paise: int
    incremental_recovered_paise: int
    intervention_cost_paise: int
    llm_cost_paise: int
    net_value_created_paise: int
    contacts: int
    contacts_per_1000_rupees_recovered: float
    opt_out_rate: float
    false_chase_count: int
    suppressed_by_circuit_breaker: int
    declined_ev_non_positive: int
    escalated_to_human: int
    unresolved_exceptions: int
    estimator: str = "stratified_holdout_amount_rate"


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _arm_metrics(
    cases: list[RecoveryCase],
    attributions: dict[str, RecoveryAttribution],
) -> ArmMetrics:
    recovered = [case for case in cases if case.id in attributions]
    return ArmMetrics(
        eligible_cases=len(cases),
        recovered_cases=len(recovered),
        amount_at_risk_paise=sum(case.amount_at_risk for case in cases),
        recovered_paise=sum(attributions[case.id].amount_paise for case in recovered),
        recovery_rate=round(len(recovered) / len(cases), 6) if cases else 0.0,
    )


def _counterfactual_organic(
    treatment: list[RecoveryCase],
    holdout: list[RecoveryCase],
    attributions: dict[str, RecoveryAttribution],
) -> int:
    holdout_risk_by_stratum: dict[tuple[str, str], int] = defaultdict(int)
    holdout_recovery_by_stratum: dict[tuple[str, str], int] = defaultdict(int)
    for case in holdout:
        stratum = (str(case.leak_type), case.amount_band)
        holdout_risk_by_stratum[stratum] += case.amount_at_risk
        if case.id in attributions:
            holdout_recovery_by_stratum[stratum] += attributions[case.id].amount_paise

    total_holdout_risk = sum(holdout_risk_by_stratum.values())
    total_holdout_recovery = sum(holdout_recovery_by_stratum.values())
    fallback_rate = total_holdout_recovery / total_holdout_risk if total_holdout_risk else 0.0
    estimate = 0.0
    for case in treatment:
        stratum = (str(case.leak_type), case.amount_band)
        stratum_risk = holdout_risk_by_stratum[stratum]
        rate = (
            holdout_recovery_by_stratum[stratum] / stratum_risk
            if stratum_risk
            else fallback_rate
        )
        estimate += case.amount_at_risk * rate
    return round(estimate)


def compute_scoreboard(session: Session, run_id: str) -> Scoreboard:
    run = session.get(BatchRun, run_id)
    if run is None:
        raise LookupError(run_id)
    cases = list(
        session.scalars(
            select(RecoveryCase)
            .where(RecoveryCase.batch_run_id == run_id)
            .order_by(RecoveryCase.detected_at, RecoveryCase.id)
        )
    )
    if not cases:
        raise ValueError(f"batch run {run_id} has no cases")
    case_ids = [case.id for case in cases]
    attribution_rows = list(
        session.scalars(
            select(RecoveryAttribution).where(RecoveryAttribution.case_id.in_(case_ids))
        )
    )
    attributions = {row.case_id: row for row in attribution_rows}
    treatment_cases = [case for case in cases if case.arm == Arm.TREATMENT.value]
    holdout_cases = [case for case in cases if case.arm == Arm.HOLDOUT.value]
    treatment = _arm_metrics(treatment_cases, attributions)
    holdout = _arm_metrics(holdout_cases, attributions)

    action_cost = int(
        session.scalar(
            select(func.coalesce(func.sum(Action.cost_paise), 0)).where(
                Action.case_id.in_(case_ids), Action.executed_at.is_not(None)
            )
        )
        or 0
    )
    llm_cost = int(
        session.scalar(
            select(func.coalesce(func.sum(LLMCall.cost_paise), 0)).where(
                LLMCall.case_id.in_(case_ids)
            )
        )
        or 0
    )
    contacts = list(
        session.scalars(select(Contact).where(Contact.case_id.in_(case_ids)))
    )
    contacted_customer_ids = {contact.customer_id for contact in contacts}
    opted_out = int(
        session.scalar(
            select(func.count(Customer.id)).where(
                Customer.id.in_(contacted_customer_ids), Customer.dnc.is_(True)
            )
        )
        or 0
    ) if contacted_customer_ids else 0
    case_by_id = {case.id: case for case in cases}
    false_chases = sum(
        1
        for contact in contacts
        if case_by_id[contact.case_id].outcome == CaseOutcome.RECOVERED.value
        and case_by_id[contact.case_id].closed_at is not None
        and _aware(contact.sent_at) > _aware(case_by_id[contact.case_id].closed_at)
    )

    started_at = _aware(run.started_at)
    completed_at = _aware(run.completed_at or datetime.now(UTC))
    duration_seconds = max(1, round((completed_at - started_at).total_seconds()))
    gross = treatment.recovered_paise
    counterfactual = _counterfactual_organic(treatment_cases, holdout_cases, attributions)
    incremental = gross - counterfactual
    total_cost = action_cost + llm_cost
    merchant = session.get(Merchant, run.merchant_id)
    return Scoreboard(
        run_id=run.id,
        merchant_id=run.merchant_id,
        synthetic=bool(merchant and (merchant.policy or {}).get("synthetic", False)),
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration_seconds,
        cases_processed=len(cases),
        cases_by_leak_type=dict(
            sorted(
                {
                    leak_type: sum(case.leak_type == leak_type for case in cases)
                    for leak_type in {case.leak_type for case in cases}
                }.items()
            )
        ),
        throughput_cases_per_minute=round(len(cases) * 60 / duration_seconds, 3),
        treatment=treatment,
        holdout=holdout,
        lift_percentage_points=round(
            (treatment.recovery_rate - holdout.recovery_rate) * 100, 3
        ),
        gross_recovered_paise=gross,
        organic_holdout_paise=holdout.recovered_paise,
        counterfactual_organic_paise=counterfactual,
        incremental_recovered_paise=incremental,
        intervention_cost_paise=action_cost,
        llm_cost_paise=llm_cost,
        net_value_created_paise=incremental - total_cost,
        contacts=len(contacts),
        contacts_per_1000_rupees_recovered=(
            round(len(contacts) * 100_000 / gross, 4) if gross else 0.0
        ),
        opt_out_rate=(
            round(opted_out / len(contacted_customer_ids), 6)
            if contacted_customer_ids
            else 0.0
        ),
        false_chase_count=false_chases,
        suppressed_by_circuit_breaker=sum(
            case.state == CaseState.SUPPRESSED.value
            or case.outcome == CaseOutcome.SUPPRESSED.value
            for case in cases
        ),
        declined_ev_non_positive=sum(
            case.outcome == CaseOutcome.ABANDONED.value for case in cases
        ),
        escalated_to_human=sum(
            case.state == CaseState.ESCALATED.value
            or case.outcome == CaseOutcome.HUMAN.value
            for case in cases
        ),
        unresolved_exceptions=sum(
            case.state
            not in {
                CaseState.CLOSED.value,
                CaseState.SUPPRESSED.value,
                CaseState.STOPPED.value,
            }
            for case in cases
        ),
    )
