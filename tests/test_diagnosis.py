from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from leakproof.diagnosis import classify_payment_failure, classify_receivable, diagnose_case
from leakproof.models.db import Diagnosis, Event
from leakproof.models.domain import CaseState, LeakType
from leakproof.services import NormalizedSignal, record_signal


@pytest.mark.parametrize(
    ("evidence", "rule_id", "failure_class"),
    [
        ({"error_source": "business"}, "T1_MERCHANT_CONFIG", "MERCHANT_FAULT"),
        ({"error_reason": "card_expired"}, "T1_INSTRUMENT_DEAD", "INSTRUMENT_DEAD"),
        ({"error_reason": "insufficient_funds"}, "T1_TIMING", "TIMING"),
        (
            {"error_source": "bank", "error_step": "payment_authorization"},
            "T1_TRANSIENT",
            "TRANSIENT",
        ),
        (
            {"error_source": "customer", "error_step": "payment_authentication"},
            "T1_FRICTION",
            "FRICTION",
        ),
        ({"error_reason": "checkout_abandoned"}, "T1_CHECKOUT_FRICTION", "FRICTION"),
        (
            {"error_reason": "payment_cancelled_by_user"},
            "T1_INTENT_ABSENT",
            "INTENT_ABSENT",
        ),
        ({"error_reason": "unmapped"}, "T1_FALLBACK", "UNKNOWN"),
    ],
)
def test_tier1_ordered_rules_cover_every_branch(evidence, rule_id, failure_class):
    result = classify_payment_failure(evidence)

    assert result.rule_id == rule_id
    assert result.failure_class == failure_class


def test_tier1_first_match_wins_and_preserves_safety_over_a_later_reason():
    result = classify_payment_failure(
        {"error_source": "business", "error_reason": "insufficient_funds"}
    )

    assert result.rule_id == "T1_MERCHANT_CONFIG"
    assert result.customer_contact_allowed is False


@pytest.mark.parametrize(
    ("evidence", "amount", "rule_id", "failure_class"),
    [
        ({"disputed": True}, 1_000_000, "R1_DISPUTED", "DISPUTED"),
        (
            {"days_overdue": 22, "payer_behavior": "SLOW_BUT_GOOD"},
            18_400_000,
            "R2_SLOW_BUT_GOOD",
            "SLOW_BUT_GOOD",
        ),
        (
            {"days_overdue": 45, "payer_behavior": "USUALLY_ON_TIME"},
            2_000_000,
            "R3_GOOD_PAYER_STRETCHED",
            "CASHFLOW_STRESSED",
        ),
        (
            {"days_overdue": 12, "payer_behavior": "CHRONIC_LATE"},
            80_000_000,
            "R4_CASHFLOW_STRESSED",
            "CASHFLOW_STRESSED",
        ),
        (
            {"days_overdue": 60, "payer_behavior": "CHRONIC_LATE"},
            4_000_000,
            "R5_DELINQUENT_STRESSED",
            "DELINQUENT",
        ),
        (
            {"days_overdue": 3, "payer_behavior": "HIGH_RISK"},
            9_000_000,
            "R6_DELINQUENT_RISKY",
            "DELINQUENT",
        ),
        ({"aging_bucket": "unknown"}, 0, "R7_FALLBACK", "DELINQUENT"),
    ],
)
def test_receivable_matrix_uses_aging_history_and_invoice_size(
    evidence, amount, rule_id, failure_class
):
    result = classify_receivable(evidence, amount)

    assert result.rule_id == rule_id
    assert result.failure_class == failure_class
    assert result.evidence["aging_bucket"] in {"1-7", "8-30", "31-90", "unknown"}
    assert result.evidence["payer_history"] in {"GOOD", "STRESSED", "RISKY"}
    assert result.evidence["invoice_size"] in {"SMALL", "MEDIUM", "LARGE"}


def test_diagnosis_persists_once_and_appends_a_replayable_event(session_factory):
    signal = NormalizedSignal(
        merchant_id="merchant_diag",
        customer_id="customer_diag",
        leak_type=LeakType.PAYMENT_FAILURE,
        entity_type="payment",
        entity_id="pay_diag",
        entity_root_id="order_diag",
        amount_at_risk=250_000,
        currency="INR",
        evidence={"error_reason": "card_expired", "error_source": "customer"},
        occurred_at=datetime(2026, 8, 27, 9, tzinfo=UTC),
    )
    with session_factory() as session:
        case, _ = record_signal(session, signal)
        first = diagnose_case(session, case.id)
        repeated = diagnose_case(session, case.id)
        count = session.scalar(select(func.count()).select_from(Diagnosis))
        event_count = session.scalar(
            select(func.count()).select_from(Event).where(Event.case_id == case.id)
        )

        assert first is repeated
        assert first.rule_id == "T1_INSTRUMENT_DEAD"
        assert case.state == CaseState.DIAGNOSED
        assert count == 1
        assert event_count == 3
