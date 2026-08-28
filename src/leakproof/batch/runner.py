from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from leakproof.actuators import execute_action
from leakproof.audit.timeline import append_event
from leakproof.diagnosis import diagnose_case
from leakproof.diagnosis.tier2 import run_cohort_scan
from leakproof.measurement import compute_scoreboard
from leakproof.models.db import Action, BatchRun, Customer, Event, RecoveryCase
from leakproof.models.domain import Arm, CaseOutcome
from leakproof.policy import plan_case
from leakproof.services import PaidSignal, record_paid_signal
from leakproof.simulator.config import SimulatorParameters
from leakproof.simulator.generate import SimulationDataset
from leakproof.simulator.seed import persist_dataset


@dataclass(frozen=True)
class BatchResult:
    run_id: str
    cases_processed: int
    diagnosed: int
    planned: int
    actions_executed: int
    recoveries_observed: int
    suppressions_opened: int
    cases_suppressed: int
    replayed: bool

    def as_dict(self) -> dict[str, str | int | bool]:
        return self.__dict__


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _draw(*parts: str) -> float:
    digest = hashlib.sha256(":".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _source_evidence(session: Session, case_id: str) -> dict:
    event = session.scalar(
        select(Event)
        .where(Event.case_id == case_id, Event.kind == "DETECTED")
        .order_by(Event.seq)
    )
    return dict(event.payload.get("evidence", {})) if event is not None else {}


def _paid_signal(case: RecoveryCase, occurred_at: datetime, reason: str) -> PaidSignal:
    root_id = case.dedupe_key.removeprefix(f"pf:{case.customer_id}:")
    return PaidSignal(
        merchant_id=case.merchant_id,
        customer_id=case.customer_id,
        entity_id=case.entity_id,
        entity_root_id=root_id if case.dedupe_key.startswith("pf:") else None,
        amount_paise=case.amount_at_risk,
        currency=case.currency,
        evidence={"synthetic": True, "outcome_source": reason},
        occurred_at=occurred_at,
    )


def _close_lost(session: Session, case: RecoveryCase, *, reason: str) -> None:
    for action in session.scalars(
        select(Action).where(Action.case_id == case.id, Action.status == "pending")
    ):
        action.status = "cancelled"
    case.outcome = CaseOutcome.LOST.value
    case.closed_at = _aware(case.attribution_until)
    append_event(
        session,
        case,
        kind="CLOSED",
        payload={"outcome": CaseOutcome.LOST.value, "reason": reason},
        actor="simulation_outcome_engine",
    )
    session.commit()


def _treatment_rate(
    parameters: SimulatorParameters,
    *,
    action_type: str,
    failure_class: str,
) -> float:
    configured = getattr(parameters.treatment_effect, action_type, None)
    return float(configured.get(failure_class, 0.0)) if isinstance(configured, dict) else 0.0


def _run_case(
    session: Session,
    case: RecoveryCase,
    *,
    parameters: SimulatorParameters,
) -> tuple[int, int]:
    """Run one synthetic case chronologically through its declared outcome window."""
    if case.outcome not in {None, CaseOutcome.SUPPRESSED.value}:
        return 0, int(case.outcome == CaseOutcome.RECOVERED.value)

    evidence = _source_evidence(session, case.id)
    simulation = dict(evidence.get("simulation", {}))
    organic = dict(simulation.get("organic_recovery", {}))
    natural_at_raw = organic.get("recovery_at")
    natural_at = _aware(datetime.fromisoformat(natural_at_raw)) if natural_at_raw else None
    failure_class = str(evidence.get("failure_class", "UNKNOWN"))
    outcome_key = str(simulation.get("outcome_key", case.dedupe_key))

    if case.outcome == CaseOutcome.SUPPRESSED.value:
        if natural_at is not None and natural_at <= _aware(case.attribution_until):
            record_paid_signal(session, _paid_signal(case, natural_at, "organic_ground_truth"))
            session.commit()
            return 0, 1
        return 0, 0

    diagnose_case(session, case.id)
    plan = plan_case(session, case.id, now=_aware(case.detected_at))
    session.commit()

    if case.arm == Arm.HOLDOUT.value:
        if natural_at is not None and natural_at <= _aware(case.attribution_until):
            record_paid_signal(session, _paid_signal(case, natural_at, "organic_ground_truth"))
            session.commit()
            return 0, 1
        _close_lost(session, case, reason="holdout recovery not observed in declared window")
        return 0, 0

    if plan.status != "PLANNED":
        if natural_at is not None and natural_at <= _aware(case.attribution_until):
            record_paid_signal(session, _paid_signal(case, natural_at, "organic_ground_truth"))
            session.commit()
            return 0, 1
        return 0, 0

    executed = 0
    contacts = 0
    actions = list(
        session.scalars(
            select(Action).where(Action.case_id == case.id).order_by(Action.step_index)
        )
    )
    for action in actions:
        attempted_at = _aware(action.scheduled_for)
        if natural_at is not None and natural_at <= attempted_at:
            record_paid_signal(session, _paid_signal(case, natural_at, "organic_ground_truth"))
            session.commit()
            return executed, 1

        result = execute_action(session, action.id, now=attempted_at)
        if result.status == "rescheduled":
            session.refresh(action)
            attempted_at = _aware(action.scheduled_for)
            result = execute_action(session, action.id, now=attempted_at)
        if result.status == "succeeded":
            executed += int(not result.replayed)
            is_contact = action.action_type not in {"silent_retry", "human_handoff"}
            contacts += int(is_contact)
            effect = _treatment_rate(
                parameters,
                action_type=action.action_type,
                failure_class=failure_class,
            )
            if contacts > 1:
                effect = max(
                    0.0,
                    effect
                    + (contacts - 1)
                    * parameters.treatment_effect.fatigue_penalty_per_extra_contact,
                )
            recovered_at = attempted_at + timedelta(hours=1)
            if (
                recovered_at <= _aware(case.attribution_until)
                and _draw(
                    outcome_key,
                    action.action_type,
                    str(action.step_index),
                    "recovery",
                )
                < effect
            ):
                if natural_at is not None and natural_at < recovered_at:
                    recovered_at = natural_at
                    source = "organic_ground_truth"
                else:
                    source = f"treatment_effect:{action.action_type}"
                record_paid_signal(session, _paid_signal(case, recovered_at, source))
                session.commit()
                return executed, 1
            if (
                is_contact
                and _draw(
                    outcome_key,
                    action.action_type,
                    str(action.step_index),
                    "opt_out",
                )
                < parameters.treatment_effect.opt_out_prob_per_contact
            ):
                customer = session.get(Customer, case.customer_id)
                assert customer is not None
                customer.dnc = True
                customer.dnc_at = attempted_at
                case.outcome = CaseOutcome.LOST.value
                case.closed_at = attempted_at
                append_event(
                    session,
                    case,
                    kind="STOPPED",
                    payload={"reason": "synthetic customer opt-out", "action_id": action.id},
                    actor="simulation_outcome_engine",
                )
                for pending in actions:
                    if pending.status == "pending":
                        pending.status = "cancelled"
                session.commit()
                if natural_at is not None and natural_at <= _aware(case.attribution_until):
                    record_paid_signal(
                        session, _paid_signal(case, natural_at, "organic_ground_truth")
                    )
                    session.commit()
                    return executed, 1
                return executed, 0
        elif result.status == "deferred":
            case.outcome = CaseOutcome.HUMAN.value
            for pending in actions:
                if pending.status == "pending":
                    pending.status = "cancelled"
            session.commit()
            if natural_at is not None and natural_at <= _aware(case.attribution_until):
                record_paid_signal(session, _paid_signal(case, natural_at, "organic_ground_truth"))
                session.commit()
                return executed, 1
            return executed, 0

    if natural_at is not None and natural_at <= _aware(case.attribution_until):
        record_paid_signal(session, _paid_signal(case, natural_at, "organic_ground_truth"))
        session.commit()
        return executed, 1
    _close_lost(session, case, reason="recovery not observed after bounded ladder")
    return executed, 0


def run_full_batch(
    session: Session,
    dataset: SimulationDataset,
    parameters: SimulatorParameters,
) -> BatchResult:
    """Execute the reproducible September 4 simulation batch end to end."""
    persist_dataset(session, dataset)
    run = session.get(BatchRun, dataset.run_id)
    assert run is not None
    cases = list(
        session.scalars(
            select(RecoveryCase)
            .where(RecoveryCase.batch_run_id == dataset.run_id)
            .order_by(RecoveryCase.detected_at, RecoveryCase.id)
        )
    )
    replayed = bool(cases) and all(case.outcome is not None for case in cases)
    if replayed:
        scoreboard = compute_scoreboard(session, dataset.run_id)
        return BatchResult(
            run_id=dataset.run_id,
            cases_processed=len(cases),
            diagnosed=0,
            planned=0,
            actions_executed=0,
            recoveries_observed=(
                scoreboard.treatment.recovered_cases + scoreboard.holdout.recovered_cases
            ),
            suppressions_opened=0,
            cases_suppressed=0,
            replayed=True,
        )

    run.started_at = datetime.now(UTC)
    outage = [item for item in dataset.signals if item.scenario == "issuer_outage"]
    cohort = run_cohort_scan(
        session,
        merchant_id=dataset.merchant_id,
        window_from=min(item.occurred_at for item in outage),
        window_to=max(item.occurred_at for item in outage) + timedelta(seconds=1),
        batch_run_id=dataset.run_id,
    )

    actions_executed = 0
    recoveries = 0
    diagnosed = 0
    planned = 0
    for case in cases:
        if case.outcome not in {None, CaseOutcome.SUPPRESSED.value}:
            continue
        had_diagnosis = session.scalar(
            select(Event.id).where(Event.case_id == case.id, Event.kind == "DIAGNOSED")
        ) is not None
        executed, recovered = _run_case(session, case, parameters=parameters)
        actions_executed += executed
        recoveries += recovered
        has_diagnosis = session.scalar(
            select(Event.id).where(Event.case_id == case.id, Event.kind == "DIAGNOSED")
        ) is not None
        diagnosed += int(not had_diagnosis and has_diagnosis)
        planned += int(
            session.scalar(
                select(Event.id).where(Event.case_id == case.id, Event.kind == "PLANNED")
            )
            is not None
        )

    run.completed_at = datetime.now(UTC)
    session.commit()
    return BatchResult(
        run_id=dataset.run_id,
        cases_processed=len(cases),
        diagnosed=diagnosed,
        planned=planned,
        actions_executed=actions_executed,
        recoveries_observed=recoveries,
        suppressions_opened=cohort.suppressions_opened,
        cases_suppressed=cohort.cases_suppressed,
        replayed=False,
    )
