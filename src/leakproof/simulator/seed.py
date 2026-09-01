from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from leakproof.audit.timeline import append_event
from leakproof.config import get_measurement_config
from leakproof.models.db import (
    BatchRun,
    Customer,
    Event,
    Merchant,
    PaymentAttemptObservation,
    RecoveryCase,
)
from leakproof.services import amount_band, assigned_arm, dedupe_key, record_signal
from leakproof.simulator.generate import SimulationDataset


@dataclass(frozen=True)
class PersistedSimulation:
    merchant_id: str
    customers_created: int
    customers_existing: int
    cases_created: int
    cases_existing: int
    events_appended: int

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)


def persist_dataset(session: Session, dataset: SimulationDataset) -> PersistedSimulation:
    """Seed the shared case spine without mutating or duplicating an earlier seeded run."""
    merchant = session.get(Merchant, dataset.merchant_id)
    if merchant is None:
        merchant = Merchant(
            id=dataset.merchant_id,
            name=dataset.merchant_name,
            policy={
                "synthetic": True,
                "simulation_run_id": dataset.run_id,
                "simulation_seed": dataset.seed,
                "simulation_parameter_sha256": dataset.parameter_sha256,
            },
        )
        session.add(merchant)
        session.flush()

    batch_run = session.get(BatchRun, dataset.run_id)
    batch_created = batch_run is None
    if batch_created:
        measurement = get_measurement_config()
        batch_run = BatchRun(
            id=dataset.run_id,
            merchant_id=dataset.merchant_id,
            started_at=datetime.now(UTC),
            completed_at=None,
            holdout_seed=measurement.holdout.seed,
            holdout_fraction=measurement.holdout.fraction,
            measurement_config=measurement.model_dump(mode="json"),
        )
        session.add(batch_run)
        session.flush()

    existing_customer_ids = set(
        session.scalars(select(Customer.id).where(Customer.merchant_id == dataset.merchant_id))
    )
    new_customers = [
        Customer(
            id=profile.id,
            merchant_id=dataset.merchant_id,
            segment=profile.segment,
            locale=profile.locale,
            protected=profile.protected,
            dnc=profile.dnc,
            dnc_at=dataset.as_of if profile.dnc else None,
            created_at=dataset.as_of,
        )
        for profile in dataset.customers
        if profile.id not in existing_customer_ids
    ]
    if new_customers:
        session.add_all(new_customers)
        session.flush()

    existing_cases = {
        case.dedupe_key: case
        for case in session.scalars(
            select(RecoveryCase).where(RecoveryCase.merchant_id == dataset.merchant_id)
        )
    }
    assigned_case_ids = (
        set(
            session.scalars(
                select(Event.case_id).where(
                    Event.case_id.in_([case.id for case in existing_cases.values()]),
                    Event.kind == "ASSIGNED",
                )
            )
        )
        if existing_cases
        else set()
    )
    created_cases = 0
    events_appended = 0
    for simulated in dataset.signals:
        signal = simulated.normalized(dataset.run_id)
        key = dedupe_key(signal)
        existing = existing_cases.get(key)
        if existing is not None:
            existing.batch_run_id = existing.batch_run_id or dataset.run_id
            existing.amount_band = amount_band(signal.amount_at_risk)
            if existing.id not in assigned_case_ids:
                assignment = assigned_arm(
                    existing.merchant_id,
                    existing.customer_id,
                    signal.leak_type,
                    signal.amount_at_risk,
                    (signal.evidence.get("simulation") or {}).get("assignment_key"),
                )
                existing.arm = assignment.arm.value
                append_event(
                    session,
                    existing,
                    kind="ASSIGNED",
                    payload={
                        "arm": assignment.arm.value,
                        "bucket": assignment.bucket,
                        "random_value": assignment.bucket / 10_000,
                        "holdout_fraction": assignment.fraction,
                        "seed": assignment.seed,
                        "stratum": assignment.stratum,
                        "stratify_by": ["leak_type", "amount_band"],
                        "backfilled": True,
                    },
                    actor="holdout_allocator",
                )
                assigned_case_ids.add(existing.id)
                events_appended += 1
            continue
        case, created = record_signal(session, signal)
        existing_cases[key] = case
        created_cases += int(created)
        events_appended += 2 * int(created)

    if batch_created:
        assert batch_run is not None
        batch_run.completed_at = datetime.now(UTC)
    existing_attempt_keys = set(
        session.scalars(
            select(PaymentAttemptObservation.attempt_key).where(
                PaymentAttemptObservation.merchant_id == dataset.merchant_id,
                PaymentAttemptObservation.provider == "simulator",
                PaymentAttemptObservation.namespace == dataset.run_id,
            )
        )
    )
    session.add_all(
        PaymentAttemptObservation(
            merchant_id=dataset.merchant_id,
            provider="simulator",
            namespace=dataset.run_id,
            attempt_key=item.attempt_key,
            provider_event_key=item.provider_event_key,
            provider_payment_id=item.provider_payment_id,
            provider_order_id=item.provider_order_id,
            observed_at=item.observed_at,
            outcome=item.outcome,
            method=item.method,
            issuer=item.issuer,
            bin_bucket=item.bin_bucket,
            checkout_step=item.checkout_step,
            checkout_version=item.checkout_version,
            error_reason=item.error_reason,
            source="synthetic",
        )
        for item in dataset.attempt_observations
        if item.attempt_key not in existing_attempt_keys
    )
    session.commit()
    return PersistedSimulation(
        merchant_id=dataset.merchant_id,
        customers_created=len(new_customers),
        customers_existing=len(existing_customer_ids),
        cases_created=created_cases,
        cases_existing=len(dataset.signals) - created_cases,
        events_appended=events_appended,
    )
