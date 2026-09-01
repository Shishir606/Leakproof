from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from random import Random
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from leakproof.models.domain import LeakType
from leakproof.services import NormalizedSignal
from leakproof.simulator.config import OrganicRecoveryConfig, SimulatorParameters
from leakproof.simulator.scenarios import (
    SCENARIO_NAMES,
    ScenarioSignal,
    build_breadth_signals,
    build_injected_scenarios,
)

PARAMETERS_PATH = Path("simulator/params.yaml")
SIMULATOR_SCHEMA_VERSION = 4
METHODS = ("upi", "card", "netbanking", "wallet", "emandate")
METHOD_WEIGHTS = (0.42, 0.30, 0.14, 0.09, 0.05)
ISSUERS = ("HDFC", "ICICI", "SBI", "AXIS", "KOTAK")
PAYER_BEHAVIORS = ("SLOW_BUT_GOOD", "USUALLY_ON_TIME", "CHRONIC_LATE", "HIGH_RISK")
PAYER_WEIGHTS = (0.43, 0.37, 0.14, 0.06)


class SimulatedCustomer(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    segment: str
    locale: str
    protected: bool
    dnc: bool
    primary_method: str
    issuer: str
    has_subscription: bool
    monthly_order_counts: list[int]
    historical_order_count: int
    historical_order_value_paise: int
    payer_behavior: str | None = None
    invoice_days_late: list[int] = Field(default_factory=list)
    historical_invoice_payment_rate: float | None = None


class OrganicOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    baseline_rate: float
    will_recover_without_intervention: bool
    delay_days: float | None
    recovery_at: datetime | None


class SimulatedPaymentAttempt(BaseModel):
    """A synthetic aggregate fact; deliberately contains no customer identifier."""

    model_config = ConfigDict(frozen=True)

    attempt_key: str
    provider_event_key: str
    provider_payment_id: str | None = None
    provider_order_id: str | None = None
    observed_at: datetime
    outcome: str
    method: str = "unknown"
    issuer: str = "unknown"
    bin_bucket: str = "unknown"
    checkout_step: str = "unknown"
    checkout_version: str = "unknown"
    error_reason: str = "unknown"


class SimulatedSignal(BaseModel):
    model_config = ConfigDict(frozen=True)

    merchant_id: str
    customer_id: str
    leak_type: LeakType
    entity_type: str
    entity_id: str
    entity_root_id: str | None
    amount_at_risk: int
    currency: str
    failure_class: str
    scenario: str
    assignment_key: str
    outcome_key: str
    evidence: dict[str, Any]
    occurred_at: datetime
    organic_recovery: OrganicOutcome

    def normalized(self, simulation_run_id: str) -> NormalizedSignal:
        evidence = {
            **self.evidence,
            "failure_class": self.failure_class,
            "simulation": {
                "synthetic": True,
                "run_id": simulation_run_id,
                "scenario": self.scenario,
                "assignment_key": self.assignment_key,
                "outcome_key": self.outcome_key,
                "organic_recovery": self.organic_recovery.model_dump(mode="json"),
            },
        }
        return NormalizedSignal(
            merchant_id=self.merchant_id,
            customer_id=self.customer_id,
            leak_type=self.leak_type,
            entity_type=self.entity_type,
            entity_id=self.entity_id,
            entity_root_id=self.entity_root_id,
            amount_at_risk=self.amount_at_risk,
            currency=self.currency,
            evidence=evidence,
            occurred_at=self.occurred_at,
        )


class SimulationDataset(BaseModel):
    model_config = ConfigDict(frozen=True)

    synthetic: bool = True
    simulator_schema_version: int = SIMULATOR_SCHEMA_VERSION
    run_id: str
    seed: int
    as_of: datetime
    merchant_id: str
    merchant_name: str
    months_of_history: int
    parameter_sha256: str
    treatment_effects: dict[str, Any]
    customers: list[SimulatedCustomer]
    signals: list[SimulatedSignal]
    attempt_observations: list[SimulatedPaymentAttempt] = Field(default_factory=list)

    def fingerprint(self) -> str:
        serialized = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(serialized).hexdigest()

    def summary(self) -> dict[str, Any]:
        leak_counts = Counter(signal.leak_type.value for signal in self.signals)
        scenario_counts = Counter(signal.scenario for signal in self.signals)
        organic_counts = Counter(
            signal.leak_type.value
            for signal in self.signals
            if signal.organic_recovery.will_recover_without_intervention
        )
        return {
            "synthetic": True,
            "simulator_schema_version": self.simulator_schema_version,
            "run_id": self.run_id,
            "seed": self.seed,
            "as_of": self.as_of.isoformat(),
            "merchant_id": self.merchant_id,
            "customers": len(self.customers),
            "b2b_invoice_customers": sum(customer.segment == "B2B" for customer in self.customers),
            "months_of_history": self.months_of_history,
            "historical_orders": sum(
                customer.historical_order_count for customer in self.customers
            ),
            "at_risk_cases": len(self.signals),
            "payment_attempt_observations": len(self.attempt_observations),
            "amount_at_risk_paise": sum(signal.amount_at_risk for signal in self.signals),
            "leak_type_counts": dict(sorted(leak_counts.items())),
            "scenario_counts": {scenario: scenario_counts[scenario] for scenario in SCENARIO_NAMES},
            "baseline_breadth_cases": scenario_counts["baseline"],
            "organic_recovery_count": sum(organic_counts.values()),
            "organic_recovery_by_leak_type": {
                leak_type.value: organic_counts[leak_type.value] for leak_type in LeakType
            },
            "parameter_sha256": self.parameter_sha256,
            "dataset_sha256": self.fingerprint(),
        }

    def artifact(self) -> dict[str, Any]:
        return {"summary": self.summary(), "dataset": self.model_dump(mode="json")}


def load_parameters(path: Path | str = PARAMETERS_PATH) -> SimulatorParameters:
    with Path(path).open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    return SimulatorParameters.model_validate(raw)


def _poisson(rng: Random, rate: float) -> int:
    limit = math.exp(-rate)
    probability = 1.0
    count = 0
    while probability > limit:
        count += 1
        probability *= rng.random()
    return count - 1


def _amount_paise(rng: Random, *, mu: float, sigma: float) -> int:
    # Source distributions describe rupee magnitudes; every persisted amount is paise.
    return max(100, round(rng.lognormvariate(mu, sigma) * 100))


def _payer_history(rng: Random, behavior: str, months: int) -> tuple[list[int], float]:
    shape = {
        "SLOW_BUT_GOOD": (3.0, 6.0, 1.0),
        "USUALLY_ON_TIME": (1.0, 2.0, 0.99),
        "CHRONIC_LATE": (3.0, 12.0, 0.86),
        "HIGH_RISK": (4.0, 14.0, 0.62),
    }
    k, theta, payment_probability = shape[behavior]
    late_days = [min(120, round(rng.gammavariate(k, theta))) for _ in range(months)]
    paid_invoices = sum(rng.random() < payment_probability for _ in range(months))
    return late_days, round(paid_invoices / months, 3)


def _generate_customers(
    parameters: SimulatorParameters,
    *,
    rng: Random,
    run_suffix: str,
) -> list[SimulatedCustomer]:
    customers: list[SimulatedCustomer] = []
    for index in range(parameters.scale.customers):
        is_b2b = index < parameters.scale.b2b_invoice_customers
        monthly_counts = [
            _poisson(rng, parameters.scale.orders_per_customer_per_month.rate)
            for _ in range(parameters.scale.months)
        ]
        historical_order_count = sum(monthly_counts)
        amount_parameters = parameters.amounts.b2c_order_paise
        historical_order_value = sum(
            _amount_paise(rng, mu=amount_parameters.mu, sigma=amount_parameters.sigma)
            for _ in range(historical_order_count)
        )
        behavior = rng.choices(PAYER_BEHAVIORS, weights=PAYER_WEIGHTS)[0] if is_b2b else None
        late_days, payment_rate = (
            _payer_history(rng, behavior, parameters.scale.months)
            if behavior is not None
            else ([], None)
        )
        customers.append(
            SimulatedCustomer(
                id=f"simcust_{run_suffix}_{index:05d}",
                segment="B2B" if is_b2b else "B2C",
                locale="hi-IN" if rng.random() < 0.24 else "en-IN",
                protected=rng.random() < 0.02,
                dnc=rng.random() < 0.01,
                primary_method=rng.choices(METHODS, weights=METHOD_WEIGHTS)[0],
                issuer=rng.choice(ISSUERS),
                has_subscription=rng.random() < parameters.scale.subscriptions_pct,
                monthly_order_counts=monthly_counts,
                historical_order_count=historical_order_count,
                historical_order_value_paise=historical_order_value,
                payer_behavior=behavior,
                invoice_days_late=late_days,
                historical_invoice_payment_rate=payment_rate,
            )
        )
    return customers


def _organic_outcome(
    rng: Random,
    config: OrganicRecoveryConfig,
    occurred_at: datetime,
) -> OrganicOutcome:
    will_recover = rng.random() < config.rate
    if not will_recover:
        return OrganicOutcome(
            baseline_rate=config.rate,
            will_recover_without_intervention=False,
            delay_days=None,
            recovery_at=None,
        )

    if config.delay_days is None:
        delay_days = round(rng.uniform(0.25, 14), 4)
    else:
        delay_days = round(rng.gammavariate(config.delay_days.k, config.delay_days.theta), 4)
    return OrganicOutcome(
        baseline_rate=config.rate,
        will_recover_without_intervention=True,
        delay_days=delay_days,
        recovery_at=occurred_at + timedelta(days=delay_days),
    )


def _signal_amount(rng: Random, parameters: SimulatorParameters, signal: ScenarioSignal) -> int:
    if signal.leak_type == LeakType.SUBSCRIPTION_HALT:
        choices = parameters.amounts.subscription_paise
        return rng.choices(choices.values, weights=choices.weights)[0]
    if signal.leak_type == LeakType.INVOICE_OVERDUE:
        distribution = parameters.amounts.b2b_invoice_paise
    else:
        distribution = parameters.amounts.b2c_order_paise
    return _amount_paise(rng, mu=distribution.mu, sigma=distribution.sigma)


def _materialize_signal(
    *,
    parameters: SimulatorParameters,
    customer: SimulatedCustomer,
    spec: ScenarioSignal,
    merchant_id: str,
    run_suffix: str,
    stable_suffix: str,
    sequence: int,
    rng: Random,
) -> SimulatedSignal:
    entity_prefix = {
        "payment": "pay",
        "invoice": "inv",
        "order": "order",
        "subscription": "sub",
        "token": "token",
    }[spec.entity_type]
    entity_id = f"{entity_prefix}_sim_{run_suffix}_{sequence:05d}"
    entity_root_id = (
        f"order_sim_{run_suffix}_{sequence:05d}"
        if spec.leak_type == LeakType.PAYMENT_FAILURE
        else None
    )
    stable_customer_id = f"simcust_{stable_suffix}_{spec.customer_index:05d}"
    stable_entity_id = f"{entity_prefix}_sim_{stable_suffix}_{sequence:05d}"
    stable_root_id = (
        f"order_sim_{stable_suffix}_{sequence:05d}"
        if spec.leak_type == LeakType.PAYMENT_FAILURE
        else None
    )
    if spec.leak_type == LeakType.PAYMENT_FAILURE:
        outcome_key = f"pf:{stable_customer_id}:{stable_root_id}"
    elif spec.leak_type == LeakType.CHECKOUT_ABANDON:
        outcome_key = f"ca:{stable_entity_id}"
    elif spec.leak_type == LeakType.SUBSCRIPTION_HALT:
        outcome_key = f"sh:{stable_entity_id}:{spec.evidence.get('cycle_number', 'unknown')}"
    elif spec.leak_type == LeakType.INVOICE_OVERDUE:
        outcome_key = f"io:{stable_entity_id}"
    else:
        outcome_key = f"mb:{stable_entity_id}"
    evidence = dict(spec.evidence)
    if spec.leak_type == LeakType.INVOICE_OVERDUE:
        evidence.update(
            {
                "payer_behavior": customer.payer_behavior,
                "prior_invoices": parameters.scale.months,
                "historical_payment_rate": customer.historical_invoice_payment_rate,
                "average_days_late": round(
                    sum(customer.invoice_days_late) / len(customer.invoice_days_late), 1
                ),
            }
        )
    return SimulatedSignal(
        merchant_id=merchant_id,
        customer_id=customer.id,
        leak_type=spec.leak_type,
        entity_type=spec.entity_type,
        entity_id=entity_id,
        entity_root_id=entity_root_id,
        amount_at_risk=_signal_amount(rng, parameters, spec),
        currency="INR",
        failure_class=spec.failure_class,
        scenario=spec.scenario,
        assignment_key=f"merchant_sim_{stable_suffix}:{stable_customer_id}",
        outcome_key=outcome_key,
        evidence=evidence,
        occurred_at=spec.occurred_at,
        organic_recovery=_organic_outcome(
            rng,
            parameters.organic_recovery[spec.failure_class],
            spec.occurred_at,
        ),
    )


def generate_dataset(
    parameters: SimulatorParameters | None = None,
    *,
    seed: int | None = None,
) -> SimulationDataset:
    parameters = parameters or load_parameters()
    if seed is not None:
        parameters = parameters.model_copy(
            update={"simulation": parameters.simulation.model_copy(update={"seed": seed})}
        )
    serialized_parameters = json.dumps(
        parameters.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    parameter_hash = hashlib.sha256(serialized_parameters).hexdigest()
    # Sensitivity scenarios must be paired: holdout assignment and outcome draws stay fixed
    # while only the treatment-effect threshold changes. The full parameter hash still creates
    # a distinct persisted run namespace for every multiplier.
    paired_parameters = parameters.model_dump(mode="json", by_alias=True)
    paired_parameters.pop("treatment_effect", None)
    paired_hash = hashlib.sha256(
        json.dumps(paired_parameters, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    # Version the synthetic identity namespace as well as the parameter hash. This keeps a
    # persistent database from silently reusing cases created by an older simulator contract.
    stable_suffix = f"{parameters.simulation.seed}_{paired_hash[:8]}"
    run_suffix = f"v{SIMULATOR_SCHEMA_VERSION}_{parameters.simulation.seed}_{parameter_hash[:8]}"
    run_id = f"sim_{run_suffix}"
    merchant_id = f"merchant_{run_id}"
    rng = Random(parameters.simulation.seed)
    customers = _generate_customers(parameters, rng=rng, run_suffix=run_suffix)
    subscription_indexes = [
        index for index, customer in enumerate(customers) if customer.has_subscription
    ]
    scenario_specs = build_injected_scenarios(parameters)
    breadth_specs = build_breadth_signals(
        parameters,
        subscription_customer_indexes=subscription_indexes,
        rng=rng,
    )
    signals = [
        _materialize_signal(
            parameters=parameters,
            customer=customers[spec.customer_index],
            spec=spec,
            merchant_id=merchant_id,
            run_suffix=run_suffix,
            stable_suffix=stable_suffix,
            sequence=sequence,
            rng=rng,
        )
        for sequence, spec in enumerate([*scenario_specs, *breadth_specs])
    ]
    outage = [item for item in signals if item.scenario == "issuer_outage"]
    outage_config = parameters.scenarios.issuer_outage
    current_attempts = round(len(outage) / outage_config.failure_rate)
    attempt_observations = [
        SimulatedPaymentAttempt(
            attempt_key=f"payment:{item.entity_id}",
            provider_event_key=f"sim_evt_failure_{index}",
            provider_payment_id=item.entity_id,
            provider_order_id=item.entity_root_id,
            observed_at=item.occurred_at,
            outcome="failure",
            method=outage_config.method,
            issuer=outage_config.issuer,
            checkout_step="payment_authorization",
            error_reason="gateway_technical_error",
        )
        for index, item in enumerate(outage)
    ]
    outage_start = min(item.occurred_at for item in outage)
    outage_end = max(item.occurred_at for item in outage)
    for index in range(current_attempts - len(outage)):
        observed_at = outage_start + (outage_end - outage_start) * (
            (index + 1) / (current_attempts - len(outage) + 1)
        )
        attempt_observations.append(
            SimulatedPaymentAttempt(
                attempt_key=f"success:order:sim_outage_success_{index}",
                provider_event_key=f"sim_evt_success_{index}",
                provider_payment_id=f"pay_sim_outage_success_{index}",
                provider_order_id=f"order_sim_outage_success_{index}",
                observed_at=observed_at,
                outcome="success",
                method=outage_config.method,
                issuer=outage_config.issuer,
            )
        )
    # A separately observed historical comparison window makes the injected incident
    # reproducible without declaring a rate or deriving a denominator from failure cases.
    baseline_attempts = 100
    baseline_failures = 4
    for index in range(baseline_attempts):
        failed = index < baseline_failures
        attempt_observations.append(
            SimulatedPaymentAttempt(
                attempt_key=f"baseline:{index}",
                provider_event_key=f"sim_evt_baseline_{index}",
                observed_at=outage_start - timedelta(days=1) + timedelta(seconds=index),
                outcome="failure" if failed else "success",
                method=outage_config.method,
                issuer=outage_config.issuer,
                checkout_step="payment_authorization" if failed else "unknown",
                error_reason="gateway_technical_error" if failed else "unknown",
            )
        )
    return SimulationDataset(
        run_id=run_id,
        seed=parameters.simulation.seed,
        as_of=parameters.simulation.as_of.astimezone(UTC),
        merchant_id=merchant_id,
        merchant_name=parameters.simulation.merchant_name,
        months_of_history=parameters.scale.months,
        parameter_sha256=parameter_hash,
        treatment_effects=parameters.treatment_effect.model_dump(mode="json"),
        customers=customers,
        signals=signals,
        attempt_observations=attempt_observations,
    )
