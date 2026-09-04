from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from leakproof.config import LadderConfig, LadderStepConfig, get_policy_config
from leakproof.diagnosis import diagnose_case
from leakproof.models.db import Event
from leakproof.models.domain import CaseOutcome, CaseState, LeakType
from leakproof.policy import (
    FixedPriorPolicy,
    Planner,
    PlanningCase,
    PlanningConstraints,
    next_retry,
    plan_case,
)
from leakproof.services import NormalizedSignal, record_signal

NOW = datetime(2026, 8, 27, 4, 30, tzinfo=UTC)  # 10:00 IST


def _action(key: str):
    return next(action for action in get_policy_config().actions if action.key == key)


def test_fixed_prior_policy_uses_segment_then_class_then_default_cells():
    policy = FixedPriorPolicy()

    segment = policy.estimate("SLOW_BUT_GOOD", "B2B", "voice_hinglish")
    class_level = policy.estimate("TRANSIENT", "B2C", "silent_retry")
    fallback = policy.estimate("UNKNOWN", "B2C", "email_link")

    assert segment.probability == 0.6
    assert segment.source == "segment:SLOW_BUT_GOOD:B2B:voice_hinglish"
    assert class_level.probability == 0.7
    assert class_level.source == "class:TRANSIENT:silent_retry"
    assert fallback.probability == 0.2
    assert fallback.source == "defaults"
    assert segment.exploratory and class_level.exploratory and fallback.exploratory


def test_ev_formula_is_deterministic_and_records_each_component():
    policy = FixedPriorPolicy()

    retry = policy.score(
        _action("silent_retry"),
        failure_class="TRANSIENT",
        segment="B2C",
        amount_at_risk_paise=1_000_000,
    )
    friction = policy.score(
        _action("alt_method_prompt"),
        failure_class="FRICTION",
        segment="B2C",
        amount_at_risk_paise=1_000_000,
    )

    assert retry.expected_recovery_paise == 700_000
    assert retry.annoyance_cost_paise == 0
    assert retry.ev_paise == 700_000
    assert friction.expected_recovery_paise == 600_000
    assert friction.annoyance_cost_paise == 20_000
    assert friction.ev_paise == 580_000
    assert policy.score(
        _action("silent_retry"),
        failure_class="TRANSIENT",
        segment="B2C",
        amount_at_risk_paise=1_000_000,
    ) == retry


def test_ev_policy_uses_merchant_margin_and_annoyance_lambda_overrides():
    score = FixedPriorPolicy().score(
        _action("email_link"),
        failure_class="FRICTION",
        segment="B2C",
        amount_at_risk_paise=1_000_000,
        margin=0.5,
        annoyance_lambda=0.1,
    )

    assert score.expected_recovery_paise == 200_000
    assert score.annoyance_cost_paise == 200_000
    assert score.ev_paise == -10


def test_retry_timing_is_bounded_and_class_specific():
    assert next_retry("TRANSIENT", 0, NOW) == NOW + timedelta(hours=6)
    assert next_retry("TRANSIENT", 1, NOW) == NOW + timedelta(hours=24)
    assert next_retry("TRANSIENT", 2, NOW) == NOW + timedelta(hours=72)
    assert next_retry("TRANSIENT", 3, NOW) is None
    assert next_retry("FRICTION", 0, NOW) == NOW + timedelta(minutes=30)
    assert next_retry("FRICTION", 1, NOW) is None
    assert next_retry("INSTRUMENT_DEAD", 0, NOW) is None


def test_timing_retry_targets_the_next_indian_payday():
    retry = next_retry("TIMING", 0, NOW)
    after_month_end = datetime(2026, 8, 31, 7, tzinfo=UTC)  # 12:30 IST
    next_month = next_retry("TIMING", 0, after_month_end)

    assert retry.astimezone().tzinfo is not None
    assert retry.astimezone(UTC) == datetime(2026, 8, 31, 4, 30, tzinfo=UTC)
    assert next_month.astimezone(UTC) == datetime(2026, 9, 1, 4, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("leak_type", "failure_class", "segment"),
    [
        (LeakType.PAYMENT_FAILURE, "TRANSIENT", "B2C"),
        (LeakType.CHECKOUT_ABANDON, "FRICTION", "B2C"),
        (LeakType.SUBSCRIPTION_HALT, "TIMING", "B2C"),
        (LeakType.INVOICE_OVERDUE, "SLOW_BUT_GOOD", "B2B"),
    ],
)
def test_every_leak_type_renders_a_bounded_positive_ev_plan(
    leak_type, failure_class, segment
):
    plan = Planner().plan(
        PlanningCase(
            case_id=f"case_{leak_type.value.lower()}",
            leak_type=leak_type,
            failure_class=failure_class,
            segment=segment,
            amount_at_risk_paise=10_000_000,
        ),
        now=NOW,
        constraints=PlanningConstraints(
            consented_channels=frozenset({"sms", "whatsapp", "voice"})
        ),
    )

    assert plan.status == "PLANNED"
    assert 0 < len(plan.steps) <= plan.max_steps
    assert all(step.ev_estimate_paise > 0 for step in plan.steps)
    assert list(step.scheduled_for for step in plan.steps) == sorted(
        step.scheduled_for for step in plan.steps
    )
    severities = [
        (_action(step.action_type).cost_paise, _action(step.action_type).intrusiveness)
        for step in plan.steps
    ]
    assert severities == sorted(severities)


def test_transient_ladder_has_three_bounded_backoff_retries_and_no_customer_contact():
    plan = Planner().plan(
        PlanningCase(
            case_id="case_transient",
            leak_type=LeakType.PAYMENT_FAILURE,
            failure_class="TRANSIENT",
            amount_at_risk_paise=1_000_000,
        ),
        now=NOW,
        constraints=PlanningConstraints(customer_contact_allowed=False),
    )

    assert [step.action_type for step in plan.steps] == ["silent_retry"] * 3
    assert [step.scheduled_for - NOW for step in plan.steps] == [
        timedelta(hours=6),
        timedelta(hours=30),
        timedelta(hours=102),
    ]
    assert not any(step.customer_facing for step in plan.steps)


def test_planner_explains_abandonment_when_no_action_has_positive_ev():
    plan = Planner().plan(
        PlanningCase(
            case_id="case_no_ev",
            leak_type=LeakType.CHECKOUT_ABANDON,
            failure_class="FRICTION",
            amount_at_risk_paise=1_000_000,
        ),
        now=NOW,
        constraints=PlanningConstraints(
            margin=0,
            annoyance_lambda=0,
            consented_channels=frozenset({"whatsapp"}),
        ),
    )

    assert plan.status == "ABANDONED"
    assert plan.steps == ()
    assert "No guardrail-eligible action has positive EV" in plan.reason
    assert all(not evaluation.selected for evaluation in plan.evaluations)
    assert all("not positive" in evaluation.reason for evaluation in plan.evaluations)


def test_planner_rejects_a_ladder_that_skips_back_to_a_cheaper_action():
    invalid = LadderConfig(
        id="bad_escalation",
        leak_type="PAYMENT_FAILURE",
        failure_classes=["TRANSIENT"],
        max_steps=2,
        steps=[
            LadderStepConfig(action="human_handoff"),
            LadderStepConfig(action="email_link"),
        ],
    )

    with pytest.raises(ValueError, match="not cheapest-first"):
        Planner(ladders=[invalid])


def _signal(*, suffix: str, error_source: str, amount: int) -> NormalizedSignal:
    return NormalizedSignal(
        merchant_id=f"merchant_{suffix}",
        customer_id=f"customer_{suffix}",
        leak_type=LeakType.PAYMENT_FAILURE,
        entity_type="payment",
        entity_id=f"pay_{suffix}",
        entity_root_id=f"order_{suffix}",
        amount_at_risk=amount,
        currency="INR",
        evidence={
            "error_source": error_source,
            "error_step": "payment_authorization",
            "error_reason": (
                "gateway_technical_error"
                if error_source == "bank"
                else "international_transaction_not_allowed"
            ),
        },
        occurred_at=NOW,
    )


def test_plan_case_appends_one_idempotent_planned_event(session_factory):
    with session_factory() as session:
        case, _ = record_signal(
            session,
            _signal(suffix="planned", error_source="bank", amount=1_000_000),
        )
        diagnose_case(session, case.id)
        first = plan_case(session, case.id, now=NOW)
        repeated = plan_case(session, case.id, now=NOW + timedelta(days=1))
        planned_events = session.scalar(
            select(func.count()).select_from(Event).where(
                Event.case_id == case.id, Event.kind == "PLANNED"
            )
        )

        assert first == repeated
        assert first.status == "PLANNED"
        assert case.state == CaseState.PLANNED
        assert planned_events == 1


def test_plan_case_closes_and_audits_an_explained_abandoned_case(session_factory):
    with session_factory() as session:
        case, _ = record_signal(
            session,
            _signal(suffix="abandoned", error_source="business", amount=100_000),
        )
        diagnose_case(session, case.id)
        result = plan_case(session, case.id, now=NOW)
        closed = session.scalar(
            select(Event).where(Event.case_id == case.id, Event.kind == "CLOSED")
        )

        assert result.status == "ABANDONED"
        assert case.state == CaseState.CLOSED
        assert case.outcome == CaseOutcome.ABANDONED
        assert case.closed_at == NOW
        assert closed.payload["outcome"] == "ABANDONED"
        assert closed.payload["reason"] == result.reason
        assert closed.payload["plan"]["evaluations"]
