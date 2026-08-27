from __future__ import annotations

from datetime import datetime
from math import isclose
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PoissonDistribution(StrictModel):
    dist: Literal["poisson"]
    rate: float = Field(alias="lambda", gt=0)


class LogNormalDistribution(StrictModel):
    dist: Literal["lognormal"]
    mu: float
    sigma: float = Field(gt=0)


class ChoiceDistribution(StrictModel):
    dist: Literal["choice"]
    values: list[int]
    weights: list[float]

    @model_validator(mode="after")
    def validate_weights(self) -> ChoiceDistribution:
        if len(self.values) != len(self.weights) or not isclose(sum(self.weights), 1):
            raise ValueError("choice values and normalized weights must have matching lengths")
        return self


class GammaDistribution(StrictModel):
    dist: Literal["gamma"]
    k: float = Field(gt=0)
    theta: float = Field(gt=0)


class SimulationConfig(StrictModel):
    seed: int = Field(ge=0)
    as_of: datetime
    merchant_name: str


class ScaleConfig(StrictModel):
    customers: int = Field(gt=0)
    months: int = Field(gt=0)
    orders_per_customer_per_month: PoissonDistribution
    subscriptions_pct: float = Field(ge=0, le=1)
    b2b_invoice_customers: int = Field(ge=0)


class AmountsConfig(StrictModel):
    b2c_order_paise: LogNormalDistribution
    subscription_paise: ChoiceDistribution
    b2b_invoice_paise: LogNormalDistribution


class FailureConfig(StrictModel):
    base_rate: float = Field(ge=0, le=1)
    class_mix: dict[str, float]

    @model_validator(mode="after")
    def validate_class_mix(self) -> FailureConfig:
        if not isclose(sum(self.class_mix.values()), 1):
            raise ValueError("failure class probabilities must sum to one")
        return self


class OrganicRecoveryConfig(StrictModel):
    rate: float = Field(ge=0, le=1)
    delay_days: GammaDistribution | None = None


class TreatmentEffectConfig(StrictModel):
    silent_retry: dict[str, float]
    whatsapp_link: dict[str, float]
    sms_link: dict[str, float]
    voice_hinglish: dict[str, float]
    fatigue_penalty_per_extra_contact: float = Field(le=0)
    opt_out_prob_per_contact: float = Field(ge=0, le=1)


class IssuerOutageConfig(StrictModel):
    failures: int = Field(gt=0)
    duration_minutes: int = Field(gt=0)
    issuer: str
    method: str
    failure_rate: float = Field(gt=0, le=1)


class ExpiredCardConfig(StrictModel):
    customers: int = Field(gt=0)
    expiry_window_days: int = Field(gt=0)


class MerchantMisconfigConfig(StrictModel):
    failures: int = Field(gt=0)
    duration_hours: int = Field(gt=0)


class PaydayClusteringConfig(StrictModel):
    failures: int = Field(gt=0)
    first_day: int = Field(ge=1, le=31)
    last_day: int = Field(ge=1, le=31)

    @model_validator(mode="after")
    def validate_days(self) -> PaydayClusteringConfig:
        if self.first_day > self.last_day:
            raise ValueError("payday clustering first day must not exceed its last day")
        return self


class InvoiceAgingConfig(StrictModel):
    overdue_invoices: int = Field(gt=0)
    max_days_overdue: int = Field(gt=0)


class ScenariosConfig(StrictModel):
    issuer_outage: IssuerOutageConfig
    expired_card_cohort: ExpiredCardConfig
    merchant_misconfig: MerchantMisconfigConfig
    payday_clustering: PaydayClusteringConfig
    invoice_aging: InvoiceAgingConfig


class BreadthConfig(StrictModel):
    checkout_abandonment: int = Field(gt=0)
    subscription_halt: int = Field(gt=0)
    mandate_broken: int = Field(gt=0)


class SimulatorParameters(StrictModel):
    simulation: SimulationConfig
    scale: ScaleConfig
    amounts: AmountsConfig
    failure: FailureConfig
    organic_recovery: dict[str, OrganicRecoveryConfig]
    treatment_effect: TreatmentEffectConfig
    scenarios: ScenariosConfig
    breadth: BreadthConfig

    @model_validator(mode="after")
    def validate_scale(self) -> SimulatorParameters:
        if self.scale.b2b_invoice_customers > self.scale.customers:
            raise ValueError("B2B invoice customer count cannot exceed total customers")
        if self.scenarios.invoice_aging.overdue_invoices > self.scale.b2b_invoice_customers:
            raise ValueError("overdue invoices require enough B2B payer profiles")
        if self.scenarios.expired_card_cohort.customers > self.scale.customers:
            raise ValueError("expired-card cohort cannot exceed total customers")
        missing = set(self.failure.class_mix) - set(self.organic_recovery)
        if missing:
            raise ValueError(f"organic recovery assumptions missing for: {sorted(missing)}")
        return self
