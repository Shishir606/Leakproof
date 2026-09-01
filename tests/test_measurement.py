from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from leakproof.config import get_measurement_config
from leakproof.diagnosis import diagnose_case
from leakproof.measurement import Scoreboard, compute_scoreboard
from leakproof.models.db import (
    Action,
    BatchRun,
    Contact,
    Event,
    Merchant,
    RecoveryAttribution,
)
from leakproof.models.domain import Arm, CaseOutcome, LeakType
from leakproof.policy import plan_case
from leakproof.services import (
    NormalizedSignal,
    PaidSignal,
    assigned_arm,
    record_paid_signal,
    record_signal,
)

NOW = datetime(2026, 8, 31, 4, 30, tzinfo=UTC)


def _signal(
    suffix: str,
    *,
    leak_type: LeakType = LeakType.PAYMENT_FAILURE,
    amount: int = 500_000,
    run_id: str | None = None,
) -> NormalizedSignal:
    return NormalizedSignal(
        merchant_id="merchant_measurement",
        customer_id=f"customer_{suffix}",
        leak_type=leak_type,
        entity_type="payment" if leak_type == LeakType.PAYMENT_FAILURE else "invoice",
        entity_id=f"entity_{suffix}",
        entity_root_id=f"order_{suffix}" if leak_type == LeakType.PAYMENT_FAILURE else None,
        amount_at_risk=amount,
        currency="INR",
        evidence={
            "error_source": "bank",
            "error_step": "payment_authorization",
            "error_reason": "gateway_technical_error",
            **(
                {"simulation": {"synthetic": True, "run_id": run_id}}
                if run_id
                else {}
            ),
        },
        occurred_at=NOW,
    )


def test_holdout_assignment_is_deterministic_and_stratified_and_audited(session_factory):
    first = assigned_arm(
        "merchant_measurement", "customer_fixed", LeakType.PAYMENT_FAILURE, 50_000
    )
    repeated = assigned_arm(
        "merchant_measurement", "customer_fixed", LeakType.PAYMENT_FAILURE, 50_000
    )
    another_stratum = assigned_arm(
        "merchant_measurement", "customer_fixed", LeakType.INVOICE_OVERDUE, 5_000_000
    )

    assert first == repeated
    assert first.stratum == "PAYMENT_FAILURE:LOW"
    assert another_stratum.stratum == "INVOICE_OVERDUE:HIGH"
    assert (first.bucket, first.stratum) != (another_stratum.bucket, another_stratum.stratum)

    with session_factory() as session:
        case, _ = record_signal(session, _signal("assignment"))
        assignment = session.scalar(
            select(Event).where(Event.case_id == case.id, Event.kind == "ASSIGNED")
        )

        assert assignment.payload["seed"] == 42
        assert assignment.payload["holdout_fraction"] == 0.1
        assert assignment.payload["stratum"] == "PAYMENT_FAILURE:MEDIUM"
        assert assignment.payload["arm"] == case.arm


def test_each_leak_type_uses_its_predeclared_attribution_window(session_factory):
    expected_days = {
        LeakType.PAYMENT_FAILURE: 7,
        LeakType.CHECKOUT_ABANDON: 7,
        LeakType.SUBSCRIPTION_HALT: 14,
        LeakType.INVOICE_OVERDUE: 21,
        LeakType.MANDATE_BROKEN: 14,
    }
    with session_factory() as session:
        for index, (leak_type, days) in enumerate(expected_days.items()):
            case, _ = record_signal(
                session,
                _signal(f"window_{index}", leak_type=leak_type),
            )
            assert case.attribution_until - case.detected_at == timedelta(days=days)


def test_holdout_is_diagnosed_and_logged_but_never_scheduled_or_contacted(session_factory):
    with session_factory() as session:
        case, _ = record_signal(session, _signal("holdout"))
        case.arm = Arm.HOLDOUT.value
        diagnose_case(session, case.id)
        plan = plan_case(session, case.id, now=NOW)

        assert plan.status == "HOLDOUT"
        assert plan.steps == ()
        assert session.scalar(
            select(func.count()).select_from(Action).where(Action.case_id == case.id)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(Contact).where(Contact.case_id == case.id)
        ) == 0


def test_paid_signal_uses_customer_amount_fallback_and_last_touch_credit(session_factory):
    with session_factory() as session:
        case, _ = record_signal(session, _signal("last_touch"))
        case.arm = Arm.TREATMENT.value
        early = Action(
            id="act_early",
            case_id=case.id,
            step_index=0,
            action_type="silent_retry",
            scheduled_for=NOW + timedelta(hours=1),
            executed_at=NOW + timedelta(hours=1),
            status="succeeded",
            cost_paise=0,
        )
        late = Action(
            id="act_late",
            case_id=case.id,
            step_index=1,
            action_type="whatsapp_link",
            scheduled_for=NOW + timedelta(hours=2),
            executed_at=NOW + timedelta(hours=2),
            status="succeeded",
            cost_paise=30,
        )
        session.add_all([early, late])
        session.flush()

        paid_case = record_paid_signal(
            session,
            PaidSignal(
                merchant_id=case.merchant_id,
                customer_id=case.customer_id,
                entity_id="different_payment_id",
                entity_root_id=None,
                amount_paise=504_000,
                currency="INR",
                evidence={"status": "captured"},
                occurred_at=NOW + timedelta(hours=3),
            ),
        )
        attribution = session.scalar(
            select(RecoveryAttribution).where(RecoveryAttribution.case_id == case.id)
        )

        assert paid_case == case
        assert attribution.matched_by == "customer_id_and_amount_within_1pct"
        assert attribution.credit_rule == "last_touch"
        assert attribution.credited_action_id == late.id
        assert attribution.credited_action_type == "whatsapp_link"
        assert attribution.organic is False


def test_payment_outside_window_stops_case_without_claiming_attribution(session_factory):
    with session_factory() as session:
        case, _ = record_signal(session, _signal("late_payment"))
        paid = record_paid_signal(
            session,
            PaidSignal(
                merchant_id=case.merchant_id,
                customer_id=case.customer_id,
                entity_id=case.entity_id,
                entity_root_id=None,
                amount_paise=case.amount_at_risk,
                currency="INR",
                evidence={},
                occurred_at=NOW + timedelta(days=8),
            ),
        )

        assert paid == case
        assert case.outcome == CaseOutcome.RECOVERED.value
        assert session.scalar(
            select(func.count()).select_from(RecoveryAttribution).where(
                RecoveryAttribution.case_id == case.id
            )
        ) == 0
        closed = session.scalar(
            select(Event).where(Event.case_id == case.id, Event.kind == "CLOSED")
        )
        assert closed.payload["attribution"]["credited"] is False


def _add_attribution(session, case, *, amount: int) -> None:
    case.outcome = CaseOutcome.RECOVERED.value
    case.closed_at = NOW + timedelta(hours=2)
    session.add(
        RecoveryAttribution(
            case_id=case.id,
            payment_entity_id=f"paid_{case.id}",
            amount_paise=amount,
            matched_by="entity_id",
            credit_rule="last_touch",
            organic=case.arm == Arm.HOLDOUT.value,
            paid_at=NOW + timedelta(hours=2),
        )
    )


def test_scoreboard_computes_lift_incremental_recovery_cost_and_api(
    session_factory, client
):
    run_id = "run_scoreboard"
    with session_factory() as session:
        config = get_measurement_config()
        # record_signal creates the merchant before the run's foreign key is inserted.
        first, _ = record_signal(session, _signal("score_0", amount=100_000, run_id=run_id))
        session.get(Merchant, first.merchant_id).policy = {"synthetic": True}
        session.add(
            BatchRun(
                id=run_id,
                merchant_id=first.merchant_id,
                started_at=NOW,
                completed_at=NOW + timedelta(minutes=10),
                holdout_seed=config.holdout.seed,
                holdout_fraction=config.holdout.fraction,
                measurement_config=config.model_dump(mode="json"),
            )
        )
        cases = [first]
        for index in range(1, 6):
            case, _ = record_signal(
                session,
                _signal(f"score_{index}", amount=100_000, run_id=run_id),
            )
            cases.append(case)
        for case in cases[:4]:
            case.arm = Arm.TREATMENT.value
        for case in cases[4:]:
            case.arm = Arm.HOLDOUT.value
        for case in cases[:3]:
            _add_attribution(session, case, amount=100_000)
        _add_attribution(session, cases[4], amount=100_000)
        session.add(
            Action(
                id="act_score_cost",
                case_id=cases[0].id,
                step_index=0,
                action_type="whatsapp_link",
                scheduled_for=NOW,
                executed_at=NOW + timedelta(minutes=1),
                status="succeeded",
                cost_paise=50,
            )
        )
        session.add(
            Contact(
                customer_id=cases[0].customer_id,
                channel="whatsapp",
                case_id=cases[0].id,
                sent_at=NOW + timedelta(minutes=1),
            )
        )
        session.commit()

        scoreboard = compute_scoreboard(session, run_id)

    assert scoreboard.cases_processed == 6
    assert scoreboard.treatment.recovery_rate == 0.75
    assert scoreboard.holdout.recovery_rate == 0.5
    assert scoreboard.lift_percentage_points == 25.0
    assert scoreboard.gross_recovered_paise == 300_000
    assert scoreboard.counterfactual_organic_paise == 200_000
    assert scoreboard.incremental_recovered_paise == 100_000
    assert scoreboard.intervention_cost_paise == 50
    assert scoreboard.incremental_revenue_paise == 100_000
    assert scoreboard.contribution_margin_paise == 68_000
    assert scoreboard.net_economic_value_paise == 67_950
    assert scoreboard.net_value_created_paise == 67_950
    assert scoreboard.seed_count == 1
    assert len(scoreboard.assumption_hash) == 64
    assert scoreboard.assumptions.contribution_margin_rate == 0.68
    assert scoreboard.uncertainty.method.startswith("single_seed_point_estimate")
    assert scoreboard.false_chase_count == 0

    response = client.get(f"/scoreboard/{run_id}")
    assert response.status_code == 200
    assert response.json()["lift_percentage_points"] == 25.0
    assert response.json()["estimator"] == "stratified_holdout_amount_rate"

    strict_payload = scoreboard.model_dump(mode="json")
    strict_payload.pop("assumptions")
    with pytest.raises(ValidationError):
        Scoreboard.model_validate(strict_payload)
    missing_provenance = scoreboard.model_dump(mode="json")
    missing_provenance.pop("data_provenance")
    with pytest.raises(ValidationError):
        Scoreboard.model_validate(missing_provenance)

    with session_factory() as session:
        run = session.get(BatchRun, run_id)
        changed = dict(run.measurement_config)
        changed["economics"] = {
            **changed["economics"],
            "contribution_margin_rate": 0.50,
        }
        run.measurement_config = changed
        session.commit()
        lower_margin = compute_scoreboard(session, run_id)

    assert lower_margin.gross_recovered_paise == scoreboard.gross_recovered_paise
    assert lower_margin.incremental_revenue_paise == scoreboard.incremental_revenue_paise
    assert lower_margin.contribution_margin_paise == 50_000
    assert lower_margin.net_economic_value_paise == 49_950


def test_scoreboard_returns_not_found_for_unknown_run(client):
    response = client.get("/scoreboard/run_missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "batch run not found"}
