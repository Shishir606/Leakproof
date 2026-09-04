"""Explicit, reviewable synthetic incidents used by the merchant simulator."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from random import Random
from typing import Any

from leakproof.models.domain import LeakType
from leakproof.simulator.config import SimulatorParameters

SCENARIO_NAMES = (
    "issuer_outage",
    "expired_card_cohort",
    "merchant_misconfig",
    "payday_clustering",
    "invoice_aging",
)


@dataclass(frozen=True)
class ScenarioSignal:
    scenario: str
    leak_type: LeakType
    failure_class: str
    entity_type: str
    customer_index: int
    occurred_at: datetime
    evidence: dict[str, Any] = field(default_factory=dict)


def _previous_month(as_of: datetime) -> tuple[int, int]:
    if as_of.month == 1:
        return as_of.year - 1, 12
    return as_of.year, as_of.month - 1


def build_injected_scenarios(parameters: SimulatorParameters) -> list[ScenarioSignal]:
    as_of = parameters.simulation.as_of.astimezone(UTC)
    scenarios = parameters.scenarios
    customer_count = parameters.scale.customers
    customer_offset = parameters.scale.b2b_invoice_customers
    signals: list[ScenarioSignal] = []

    outage_start = as_of - timedelta(hours=2)
    for index in range(scenarios.issuer_outage.failures):
        offset_seconds = index * scenarios.issuer_outage.duration_minutes * 60
        offset_seconds //= scenarios.issuer_outage.failures
        signals.append(
            ScenarioSignal(
                scenario="issuer_outage",
                leak_type=LeakType.PAYMENT_FAILURE,
                failure_class="TRANSIENT",
                entity_type="payment",
                customer_index=(customer_offset + index) % customer_count,
                occurred_at=outage_start + timedelta(seconds=offset_seconds),
                evidence={
                    "issuer": scenarios.issuer_outage.issuer,
                    "method": scenarios.issuer_outage.method,
                    "error_source": "bank",
                    "error_step": "payment_authorization",
                    "error_reason": "gateway_technical_error",
                    "incident_duration_minutes": scenarios.issuer_outage.duration_minutes,
                },
            )
        )

    customer_offset += scenarios.issuer_outage.failures
    for index in range(scenarios.expired_card_cohort.customers):
        expiry_days_ago = index % scenarios.expired_card_cohort.expiry_window_days
        signals.append(
            ScenarioSignal(
                scenario="expired_card_cohort",
                leak_type=LeakType.PAYMENT_FAILURE,
                failure_class="INSTRUMENT_DEAD",
                entity_type="payment",
                customer_index=(customer_offset + index) % customer_count,
                occurred_at=as_of - timedelta(hours=8, minutes=index),
                evidence={
                    "method": "card",
                    "error_source": "customer",
                    "error_step": "payment_authentication",
                    "error_reason": "card_expired",
                    "instrument_expired_on": (
                        as_of.date() - timedelta(days=expiry_days_ago)
                    ).isoformat(),
                },
            )
        )

    customer_offset += scenarios.expired_card_cohort.customers
    misconfig_start = as_of - timedelta(hours=scenarios.merchant_misconfig.duration_hours)
    for index in range(scenarios.merchant_misconfig.failures):
        offset_seconds = index * scenarios.merchant_misconfig.duration_hours * 3600
        offset_seconds //= scenarios.merchant_misconfig.failures
        signals.append(
            ScenarioSignal(
                scenario="merchant_misconfig",
                leak_type=LeakType.PAYMENT_FAILURE,
                failure_class="MERCHANT_FAULT",
                entity_type="payment",
                customer_index=(customer_offset + index) % customer_count,
                occurred_at=misconfig_start + timedelta(seconds=offset_seconds),
                evidence={
                    "method": "card",
                    "international": True,
                    "error_source": "business",
                    "error_step": "payment_authorization",
                    "error_reason": "international_transaction_not_allowed",
                    "merchant_configuration": "international_payments_disabled",
                    "incident_duration_hours": scenarios.merchant_misconfig.duration_hours,
                },
            )
        )

    customer_offset += scenarios.merchant_misconfig.failures
    previous_year, previous_month = _previous_month(as_of)
    month_days = monthrange(previous_year, previous_month)[1]
    last_payday = min(scenarios.payday_clustering.last_day, month_days)
    payday_span = last_payday - scenarios.payday_clustering.first_day + 1
    for index in range(scenarios.payday_clustering.failures):
        day = scenarios.payday_clustering.first_day + index % payday_span
        occurred_at = datetime(
            previous_year,
            previous_month,
            day,
            8 + index % 12,
            index % 60,
            tzinfo=UTC,
        )
        signals.append(
            ScenarioSignal(
                scenario="payday_clustering",
                leak_type=LeakType.PAYMENT_FAILURE,
                failure_class="TIMING",
                entity_type="payment",
                customer_index=(customer_offset + index) % customer_count,
                occurred_at=occurred_at,
                evidence={
                    "method": "upi",
                    "error_source": "customer",
                    "error_step": "payment_authorization",
                    "error_reason": "insufficient_funds",
                    "payday_cluster_day": day,
                },
            )
        )

    for index in range(scenarios.invoice_aging.overdue_invoices):
        overdue_days = min(1 + (index * 7) % 97, scenarios.invoice_aging.max_days_overdue)
        aging_bucket = "1-7" if overdue_days <= 7 else "8-30" if overdue_days <= 30 else "31-90"
        signals.append(
            ScenarioSignal(
                scenario="invoice_aging",
                leak_type=LeakType.INVOICE_OVERDUE,
                failure_class="invoice_overdue",
                entity_type="invoice",
                customer_index=index,
                occurred_at=as_of,
                evidence={
                    "status": "issued",
                    "days_overdue": overdue_days,
                    "aging_bucket": aging_bucket,
                    "due_by": (as_of - timedelta(days=overdue_days)).isoformat(),
                },
            )
        )

    return signals


def build_breadth_signals(
    parameters: SimulatorParameters,
    *,
    subscription_customer_indexes: list[int],
    rng: Random,
) -> list[ScenarioSignal]:
    as_of = parameters.simulation.as_of.astimezone(UTC)
    customer_count = parameters.scale.customers
    customer_offset = parameters.scale.b2b_invoice_customers + 400
    signals: list[ScenarioSignal] = []

    for index in range(parameters.breadth.checkout_abandonment):
        stale_minutes = 31 + rng.randrange(150)
        signals.append(
            ScenarioSignal(
                scenario="baseline",
                leak_type=LeakType.CHECKOUT_ABANDON,
                failure_class="FRICTION",
                entity_type="order",
                customer_index=(customer_offset + index) % customer_count,
                occurred_at=as_of - timedelta(minutes=stale_minutes),
                evidence={
                    "status": "created",
                    "minutes_without_capture": stale_minutes,
                    "error_reason": "checkout_abandoned",
                },
            )
        )

    if not subscription_customer_indexes:
        raise ValueError("subscription halt coverage requires at least one subscription customer")
    for index in range(parameters.breadth.subscription_halt):
        signals.append(
            ScenarioSignal(
                scenario="baseline",
                leak_type=LeakType.SUBSCRIPTION_HALT,
                failure_class="TIMING",
                entity_type="subscription",
                customer_index=subscription_customer_indexes[
                    index % len(subscription_customer_indexes)
                ],
                occurred_at=as_of - timedelta(hours=1 + index % 48),
                evidence={
                    "status": "halted" if index % 2 == 0 else "pending",
                    "cycle_number": 1 + index % 12,
                    "error_reason": "insufficient_funds",
                },
            )
        )

    return signals
