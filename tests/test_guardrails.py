from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from leakproof.guardrails import (
    ContactRecord,
    Gate,
    GateCase,
    GateCustomer,
    GateDiagnosis,
    GatePlan,
    GateVerdict,
    PlannedAction,
    record_gate_verdict,
)
from leakproof.messaging import TemplateRegistry
from leakproof.models.db import Action, Event
from leakproof.models.domain import LeakType
from leakproof.services import NormalizedSignal, record_signal

SAFE_TIME = datetime(2026, 8, 27, 5, tzinfo=UTC)  # 10:30 IST


@pytest.fixture
def baseline():
    return {
        "case": GateCase(merchant_id="merchant_gate"),
        "action": PlannedAction(action_type="silent_retry", scheduled_for=SAFE_TIME),
        "customer": GateCustomer(),
        "diagnosis": GateDiagnosis(failure_class="TRANSIENT"),
        "plan": GatePlan(max_steps=4),
        "suppression_matches": False,
    }


def _evaluate(values):
    return Gate().evaluate(
        values["case"],
        values["action"],
        customer=values["customer"],
        diagnosis=values["diagnosis"],
        plan=values["plan"],
        suppression_matches=values["suppression_matches"],
    )


def test_a_clear_action_passes_every_rule_and_verdict_cannot_be_constructed_directly(baseline):
    verdict = _evaluate(baseline)

    assert verdict.decision == "ALLOW"
    assert len(verdict.rules_evaluated) == 23
    assert all(rule.passed for rule in verdict.rules_evaluated)
    with pytest.raises(TypeError):
        GateVerdict("ALLOW", (), None, None)


@pytest.mark.parametrize(
    ("rule_id", "decision", "mutation"),
    [
        ("SR1_PAID", "DENY", ("case", {"verified_paid": True})),
        ("SR2_OPT_OUT", "DENY", ("customer", {"dnc": True})),
        ("SR3_DISPUTE", "DEFER_TO_HUMAN", ("case", {"disputed": True})),
        ("SR4_BUDGET", "DENY", ("case", {"attempts": 4})),
        ("SR5_SUPPRESSED", "DENY", ("suppression_matches", True)),
        (
            "SR6_MERCHANT_FAULT",
            "DENY",
            ("diagnosis", {"failure_class": "MERCHANT_FAULT"}),
        ),
        ("SR7_PROTECTED", "DEFER_TO_HUMAN", ("customer", {"protected": True})),
    ],
)
def test_each_stopping_rule_has_a_blocking_test(baseline, rule_id, decision, mutation):
    target, change = mutation
    if target == "suppression_matches":
        baseline[target] = change
    else:
        baseline[target] = replace(baseline[target], **change)
    if rule_id == "SR6_MERCHANT_FAULT":
        baseline["action"] = replace(baseline["action"], is_customer_facing=True)

    verdict = _evaluate(baseline)
    results = {rule.rule_id: rule for rule in verdict.rules_evaluated}

    assert verdict.decision == decision
    assert results[rule_id].passed is False
    assert len([key for key in results if key.startswith("SR")]) == 7


@pytest.mark.parametrize(
    "rule_id",
    [
        "SR1_PAID",
        "SR2_OPT_OUT",
        "SR3_DISPUTE",
        "SR4_BUDGET",
        "SR5_SUPPRESSED",
        "SR6_MERCHANT_FAULT",
        "SR7_PROTECTED",
    ],
)
def test_each_stopping_rule_has_an_explicit_passing_result(baseline, rule_id):
    verdict = _evaluate(baseline)
    result = next(rule for rule in verdict.rules_evaluated if rule.rule_id == rule_id)

    assert result.passed is True
    assert result.decision == "ALLOW"


def _registered_message():
    return TemplateRegistry().render(
        "util_invoice_reminder_v3",
        {
            "payer_name": "Asha",
            "invoice_no": "INV-27",
            "amount": "INR 1,000",
            "due_date": "27 Aug 2026",
            "link": "https://pay.example/registered",
        },
    )


def test_whatsapp_requires_both_consent_and_a_registered_template(baseline):
    action = replace(
        baseline["action"],
        action_type="whatsapp_link",
        is_customer_facing=True,
        channel="whatsapp",
        rendered_message=_registered_message(),
    )
    denied = Gate().evaluate(
        baseline["case"],
        action,
        customer=baseline["customer"],
        diagnosis=baseline["diagnosis"],
        plan=baseline["plan"],
    )
    allowed = Gate().evaluate(
        baseline["case"],
        replace(action, consent_granted=True, consent_basis="utility_opt_in"),
        customer=baseline["customer"],
        diagnosis=baseline["diagnosis"],
        plan=baseline["plan"],
    )

    assert denied.decision == "DENY"
    assert allowed.decision == "ALLOW"


def test_contact_cap_counts_contacts_across_cases(baseline):
    action = replace(
        baseline["action"], action_type="email_link", is_customer_facing=True, channel="email"
    )
    contacts = [
        ContactRecord(channel=f"channel-{index}", sent_at=SAFE_TIME - timedelta(days=index))
        for index in range(4)
    ]

    verdict = Gate().evaluate(
        baseline["case"],
        action,
        customer=baseline["customer"],
        diagnosis=baseline["diagnosis"],
        plan=baseline["plan"],
        contacts=contacts,
    )

    assert verdict.decision == "DENY"
    assert verdict.reason == "rolling seven-day customer contact cap reached across all cases"


def test_quiet_hours_reschedule_instead_of_denying(baseline):
    late = datetime(2026, 8, 27, 17, tzinfo=UTC)  # 22:30 IST
    action = replace(
        baseline["action"],
        action_type="email_link",
        scheduled_for=late,
        is_customer_facing=True,
        channel="email",
    )

    verdict = Gate().evaluate(
        baseline["case"],
        action,
        customer=baseline["customer"],
        diagnosis=baseline["diagnosis"],
        plan=baseline["plan"],
    )

    assert verdict.decision == "RESCHEDULE"
    assert verdict.retry_at.hour == 8
    assert verdict.retry_at.tzinfo is not None


def test_money_ceiling_and_two_key_rules_fail_closed(baseline):
    action = replace(
        baseline["action"],
        action_type="silent_retry",
        amount_paise=6_000_000,
        makes_debit=True,
        mandate_max_amount_paise=5_000_000,
    )

    verdict = Gate().evaluate(
        baseline["case"],
        action,
        customer=baseline["customer"],
        diagnosis=baseline["diagnosis"],
        plan=baseline["plan"],
    )

    assert verdict.decision == "DENY"
    failed = {rule.rule_id for rule in verdict.rules_evaluated if not rule.passed}
    assert failed >= {"MONEY_AMOUNT_CEILING", "MONEY_TWO_KEY"}


def test_message_and_tone_integrity_reject_free_text_and_threats(baseline):
    action = replace(
        baseline["action"],
        action_type="whatsapp_link",
        is_customer_facing=True,
        channel="whatsapp",
        consent_granted=True,
        contains_legal_language=True,
    )

    verdict = Gate().evaluate(
        baseline["case"],
        action,
        customer=baseline["customer"],
        diagnosis=baseline["diagnosis"],
        plan=baseline["plan"],
    )

    failed = {rule.rule_id for rule in verdict.rules_evaluated if not rule.passed}
    assert verdict.decision == "DENY"
    assert failed >= {"MESSAGE_REGISTERED_TEMPLATE", "TONE_LEGAL"}


def test_full_verdict_is_persisted_on_action_and_append_only_timeline(
    baseline, session_factory
):
    late = datetime(2026, 8, 27, 17, tzinfo=UTC)
    planned = replace(
        baseline["action"],
        action_type="email_link",
        scheduled_for=late,
        is_customer_facing=True,
        channel="email",
    )
    verdict = Gate().evaluate(
        baseline["case"],
        planned,
        customer=baseline["customer"],
        diagnosis=baseline["diagnosis"],
        plan=baseline["plan"],
    )
    signal = NormalizedSignal(
        merchant_id="merchant_audit",
        customer_id="customer_audit",
        leak_type=LeakType.PAYMENT_FAILURE,
        entity_type="payment",
        entity_id="pay_audit",
        entity_root_id="order_audit",
        amount_at_risk=100_000,
        currency="INR",
        evidence={"error_reason": "insufficient_funds"},
        occurred_at=SAFE_TIME,
    )
    with session_factory() as session:
        case, _ = record_signal(session, signal)
        action = Action(
            id="act_audit",
            case_id=case.id,
            step_index=0,
            action_type="email_link",
            scheduled_for=late,
            cost_paise=10,
        )
        session.add(action)
        record_gate_verdict(session, case, action, verdict)
        session.commit()
        event = session.scalar(
            select(Event).where(Event.case_id == case.id, Event.kind == "GATE")
        )

        assert action.verdict == "RESCHEDULE"
        assert len(action.verdict_rules["rules"]) == 23
        assert event.payload["retry_at"].endswith("+05:30")
        assert len(event.payload["rules_evaluated"]) == 23
