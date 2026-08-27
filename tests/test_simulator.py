from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from leakproof.models.db import Customer, Event, RecoveryCase
from leakproof.models.domain import LeakType
from leakproof.simulator.generate import SimulationDataset, generate_dataset, load_parameters
from leakproof.simulator.scenarios import SCENARIO_NAMES
from leakproof.simulator.seed import persist_dataset


@pytest.fixture(scope="module")
def dataset() -> SimulationDataset:
    return generate_dataset(load_parameters())


def test_simulator_assumptions_load_and_include_honest_recovery_parameters():
    parameters = load_parameters()

    assert parameters.simulation.seed == 42
    assert parameters.scale.customers == 5_000
    assert parameters.scale.months == 12
    assert parameters.scale.b2b_invoice_customers == 400
    assert parameters.organic_recovery["TIMING"].rate == 0.42
    assert parameters.organic_recovery["invoice_overdue"].rate == 0.61
    assert parameters.organic_recovery["MERCHANT_FAULT"].rate == 0
    assert parameters.treatment_effect.fatigue_penalty_per_extra_contact == -0.04
    assert parameters.treatment_effect.opt_out_prob_per_contact == 0.012


def test_fixed_seed_generates_identical_artifacts_and_another_seed_changes_them(dataset):
    repeated = generate_dataset(load_parameters())
    alternate = generate_dataset(load_parameters(), seed=43)

    assert repeated.model_dump(mode="json") == dataset.model_dump(mode="json")
    assert repeated.fingerprint() == dataset.fingerprint()
    assert alternate.fingerprint() != dataset.fingerprint()
    assert alternate.run_id != dataset.run_id


def test_seed_generates_5000_customers_and_twelve_months_of_history(dataset):
    assert len(dataset.customers) == 5_000
    assert sum(customer.segment == "B2B" for customer in dataset.customers) == 400
    assert all(len(customer.monthly_order_counts) == 12 for customer in dataset.customers)
    assert all(
        customer.historical_order_count == sum(customer.monthly_order_counts)
        for customer in dataset.customers
    )
    assert dataset.summary()["historical_orders"] > 75_000
    assert all(len(customer.invoice_days_late) == 12 for customer in dataset.customers[:400])
    assert {customer.payer_behavior for customer in dataset.customers[:400]} == {
        "SLOW_BUT_GOOD",
        "USUALLY_ON_TIME",
        "CHRONIC_LATE",
        "HIGH_RISK",
    }


def test_seed_covers_all_five_leak_types_and_at_least_500_at_risk_cases(dataset):
    counts = Counter(signal.leak_type for signal in dataset.signals)

    assert set(counts) == set(LeakType)
    assert sum(counts.values()) == 787
    assert counts == {
        LeakType.PAYMENT_FAILURE: 327,
        LeakType.CHECKOUT_ABANDON: 100,
        LeakType.SUBSCRIPTION_HALT: 100,
        LeakType.INVOICE_OVERDUE: 160,
        LeakType.MANDATE_BROKEN: 100,
    }
    assert all(signal.amount_at_risk > 0 and signal.currency == "INR" for signal in dataset.signals)


def test_all_five_scenarios_have_the_specified_incident_shapes(dataset):
    by_scenario = {
        scenario: [signal for signal in dataset.signals if signal.scenario == scenario]
        for scenario in SCENARIO_NAMES
    }
    assert {name: len(signals) for name, signals in by_scenario.items()} == {
        "issuer_outage": 47,
        "expired_card_cohort": 180,
        "merchant_misconfig": 40,
        "payday_clustering": 60,
        "invoice_aging": 160,
    }

    outage = by_scenario["issuer_outage"]
    assert {(signal.evidence["issuer"], signal.evidence["method"]) for signal in outage} == {
        ("HDFC", "netbanking")
    }
    assert max(signal.occurred_at for signal in outage) - min(
        signal.occurred_at for signal in outage
    ) < timedelta(minutes=40)
    assert all(signal.evidence["cohort_failure_rate"] == 0.9 for signal in outage)

    expired = by_scenario["expired_card_cohort"]
    expiry_dates = {
        date.fromisoformat(signal.evidence["instrument_expired_on"]) for signal in expired
    }
    assert len({signal.customer_id for signal in expired}) == 180
    assert (max(expiry_dates) - min(expiry_dates)).days < 7
    assert all(signal.failure_class == "INSTRUMENT_DEAD" for signal in expired)

    merchant_fault = by_scenario["merchant_misconfig"]
    assert all(signal.failure_class == "MERCHANT_FAULT" for signal in merchant_fault)
    assert all(signal.evidence["error_source"] == "business" for signal in merchant_fault)
    assert all(signal.evidence["international"] for signal in merchant_fault)

    payday = by_scenario["payday_clustering"]
    assert {signal.occurred_at.day for signal in payday} == {26, 27, 28, 29, 30, 31}
    assert all(signal.evidence["error_reason"] == "insufficient_funds" for signal in payday)

    invoices = by_scenario["invoice_aging"]
    assert all(signal.leak_type == LeakType.INVOICE_OVERDUE for signal in invoices)
    assert {signal.evidence["aging_bucket"] for signal in invoices} == {"1-7", "8-30", "31-90"}
    assert {signal.evidence["payer_behavior"] for signal in invoices} >= {
        "SLOW_BUT_GOOD",
        "HIGH_RISK",
    }


def test_organic_recovery_is_present_for_every_leak_type_without_fabricated_merchant_fix(dataset):
    recoveries = Counter(
        signal.leak_type
        for signal in dataset.signals
        if signal.organic_recovery.will_recover_without_intervention
    )

    assert set(recoveries) == set(LeakType)
    assert sum(recoveries.values()) > 150
    for signal in dataset.signals:
        organic = signal.organic_recovery
        if organic.will_recover_without_intervention:
            assert organic.delay_days is not None
            assert organic.delay_days > 0
            assert organic.recovery_at > signal.occurred_at
        else:
            assert organic.delay_days is None
            assert organic.recovery_at is None
    assert all(
        not signal.organic_recovery.will_recover_without_intervention
        for signal in dataset.signals
        if signal.scenario == "merchant_misconfig"
    )


def test_seed_persists_all_profiles_and_cases_idempotently_through_the_event_spine(
    dataset, session_factory
):
    with session_factory() as session:
        first = persist_dataset(session, dataset)
        first_event_count = session.scalar(select(func.count()).select_from(Event))
        repeated = persist_dataset(session, dataset)
        repeated_event_count = session.scalar(select(func.count()).select_from(Event))
        customer_count = session.scalar(select(func.count()).select_from(Customer))
        cases = list(session.scalars(select(RecoveryCase)))
        first_event = session.scalars(select(Event).order_by(Event.id)).first()

    assert first.customers_created == 5_000
    assert first.cases_created == 787
    assert first.events_appended == 1_574
    assert repeated.customers_created == 0
    assert repeated.customers_existing == 5_000
    assert repeated.cases_created == 0
    assert repeated.cases_existing == 787
    assert first_event_count == repeated_event_count == 1_574
    assert customer_count == 5_000
    assert {case.leak_type for case in cases} == set(LeakType)
    assert first_event.payload["evidence"]["simulation"]["synthetic"] is True
    assert first_event.payload["evidence"]["simulation"]["run_id"] == dataset.run_id
