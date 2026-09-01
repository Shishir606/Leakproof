from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="LEAKPROOF_", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://leakproof:leakproof@localhost:5432/leakproof"
    redis_url: str = "redis://localhost:6379/0"
    default_merchant_id: str = "merchant_demo"
    mode: Literal["simulation", "live_demo"] = "simulation"
    config_dir: Path = Path("config")

    operator_api_token: str = ""
    operator_merchant_ids: str = ""

    public_base_url: str = ""
    recovery_token_secret: str = ""
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    openai_api_key: str = ""
    openai_model: Literal["gpt-5.6-luna"] = "gpt-5.6-luna"
    openai_usd_to_inr: float = Field(default=100.0, gt=0)
    resend_api_key: str = ""
    resend_webhook_secret: str = ""
    resend_from_email: str = ""
    demo_email_allowlist: str = ""
    demo_amount_paise: int = Field(default=50_000, ge=100)
    demo_currency: Literal["INR"] = "INR"
    demo_session_ttl_minutes: int = Field(default=30, ge=5, le=120)
    demo_sessions_per_ip_hour: int = Field(default=10, ge=1, le=100)
    demo_checkout_events_per_session: int = Field(default=100, ge=4, le=1_000)
    demo_abandonment_delay_seconds: int = Field(default=7, ge=1, le=300)
    demo_sessions_enabled: bool = True
    luna_enabled: bool = True
    outbound_email_enabled: bool = True
    resend_daily_limit: int = Field(default=100, gt=0)
    resend_monthly_limit: int = Field(default=3_000, gt=0)

    @property
    def allowed_demo_emails(self) -> frozenset[str]:
        return frozenset(
            email.strip().casefold()
            for email in self.demo_email_allowlist.split(",")
            if email.strip()
        )

    @property
    def operator_merchant_scope(self) -> frozenset[str]:
        configured = frozenset(
            merchant_id.strip()
            for merchant_id in self.operator_merchant_ids.split(",")
            if merchant_id.strip()
        )
        return configured or frozenset({self.default_merchant_id})

    @model_validator(mode="after")
    def validate_live_demo_readiness(self) -> Settings:
        if self.mode == "simulation":
            return self

        missing = [
            name
            for name in (
                "public_base_url",
                "operator_api_token",
                "recovery_token_secret",
                "razorpay_key_id",
                "razorpay_key_secret",
                "resend_webhook_secret",
            )
            if not getattr(self, name).strip()
        ]
        if self.luna_enabled and not self.openai_api_key.strip():
            missing.append("openai_api_key")
        if self.outbound_email_enabled:
            missing.extend(
                name
                for name in ("resend_api_key", "resend_from_email")
                if not getattr(self, name).strip()
            )
        if missing:
            raise ValueError("live_demo configuration is incomplete: " + ", ".join(sorted(missing)))

        parsed_url = urlsplit(self.public_base_url)
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            raise ValueError("live_demo requires an HTTPS LEAKPROOF_PUBLIC_BASE_URL")
        if not self.razorpay_key_id.startswith("rzp_test_"):
            raise ValueError("live_demo requires a Razorpay test-mode key beginning with rzp_test_")
        if len(self.recovery_token_secret) < 32:
            raise ValueError("LEAKPROOF_RECOVERY_TOKEN_SECRET must contain at least 32 characters")
        if len(self.operator_api_token.encode("utf-8")) < 32:
            raise ValueError("LEAKPROOF_OPERATOR_API_TOKEN must contain at least 32 random bytes")
        if "*" in self.operator_merchant_scope:
            raise ValueError("live_demo operator merchant scope cannot use a wildcard")
        if self.resend_from_email and "@" not in self.resend_from_email:
            raise ValueError("LEAKPROOF_RESEND_FROM_EMAIL must be an email address")
        if self.resend_monthly_limit < self.resend_daily_limit:
            raise ValueError("monthly Resend limit must be greater than or equal to daily limit")
        return self


class ActionConfig(BaseModel):
    key: str
    cost_paise: int = Field(ge=0)
    intrusiveness: int = Field(ge=0, le=10)
    customer_facing: bool = False
    applicable_to: list[str] = Field(default_factory=list)
    requires_consent: bool = False
    channel: str | None = None
    two_key: bool = False


class TemplateConfig(BaseModel):
    id: str
    channel: str
    dlt_or_meta_ref: str
    variables: list[str]
    tone: str
    languages: list[str]
    content: dict[str, str]


class DiagnosisRuleConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    match: dict[str, Any]
    failure_class: str = Field(alias="class")
    confidence: float = Field(ge=0, le=1)
    customer_contact_allowed: bool | None = None
    retry_allowed: bool | None = None
    retry_strategy: str | None = None
    max_contacts: int | None = Field(default=None, ge=0)
    escalate_to_tier2: bool = False


class ReceivableRuleConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    match: dict[str, Any]
    failure_class: str = Field(alias="class")
    confidence: float = Field(ge=0, le=1)


class StoppingRuleConfig(BaseModel):
    id: str
    when: str
    decision: Literal["DENY", "DEFER_TO_HUMAN"]


class ConsentConfig(BaseModel):
    required_for: list[str]
    check: str
    decision_if_absent: Literal["DENY"]


class FrequencyConfig(BaseModel):
    max_contacts_per_customer_7d: int = Field(ge=0)
    min_hours_between_same_channel: int = Field(ge=0)
    min_hours_between_any_contact: int = Field(ge=0)


class ContactWindowConfig(BaseModel):
    start: time
    end: time


class ScheduleConfig(BaseModel):
    contact_window_ist: ContactWindowConfig
    decision_outside_window: Literal["RESCHEDULE"]
    blackout_dates: list[str]


class EMandateConfig(BaseModel):
    pre_debit_notice_hours: int = Field(ge=0)
    afa_free_ceiling_paise: int = Field(ge=0)
    afa_free_ceiling_special_paise: int = Field(ge=0)


class MoneyConfig(BaseModel):
    max_retry_attempts_per_instrument: int = Field(ge=0)
    retry_backoff_hours: list[int] = Field(min_length=1)
    never_exceed: list[str]
    emandate: EMandateConfig
    two_key_above_paise: int = Field(ge=0)
    two_key_actions: list[str]


class ToneConfig(BaseModel):
    penalty_language: dict[str, str]
    legal_language: Literal["forbidden"]
    third_party_contact: Literal["forbidden"]


class GuardrailConfig(BaseModel):
    stopping_rules: list[StoppingRuleConfig]
    consent: ConsentConfig
    frequency: FrequencyConfig
    schedule: ScheduleConfig
    money: MoneyConfig
    tone: ToneConfig


class PriorCell(BaseModel):
    alpha: float = Field(gt=0)
    beta: float = Field(gt=0)


class PriorsConfig(BaseModel):
    defaults: PriorCell
    cells: dict[str, dict[str, PriorCell]]
    segment_cells: dict[str, dict[str, dict[str, PriorCell]]] = Field(default_factory=dict)


class PolicyDefaultsConfig(BaseModel):
    margin: float = Field(default=1.0, ge=0)
    annoyance_lambda: float = Field(default=0.02, ge=0)
    exploratory_below_observations: int = Field(default=30, ge=0)


class LadderStepConfig(BaseModel):
    action: str
    delay_hours: float = Field(default=0, ge=0)


class LadderConfig(BaseModel):
    id: str
    leak_type: str
    failure_classes: list[str]
    max_steps: int = Field(gt=0)
    steps: list[LadderStepConfig] = Field(min_length=1)


class ModelTarget(BaseModel):
    model: str
    max_tokens: int | None = Field(default=None, gt=0)


class RouteConfig(BaseModel):
    primary: ModelTarget
    escalate_if: dict[str, Any] | None = None
    escalate_to: ModelTarget | None = None


class BudgetConfig(BaseModel):
    per_batch_paise: int = Field(ge=0)
    per_case_paise: int = Field(ge=0)
    alert_at_pct: float = Field(gt=0, le=1)


class ModelsConfig(BaseModel):
    routes: dict[str, RouteConfig]
    budgets: BudgetConfig


class AttributionConfig(BaseModel):
    windows_days: dict[str, int]
    credit_rule: Literal["last_touch"]
    amount_tolerance_pct: float = Field(default=1.0, ge=0, le=100)


class AmountBandsConfig(BaseModel):
    low_max: int = Field(gt=0)
    medium_max: int = Field(gt=0)

    def model_post_init(self, __context: Any) -> None:
        if self.medium_max <= self.low_max:
            raise ValueError("medium amount-band maximum must exceed low maximum")


class HoldoutConfig(BaseModel):
    fraction: float = Field(ge=0, le=1)
    stratify_by: list[Literal["leak_type", "amount_band"]]
    seed: int
    amount_bands_paise: AmountBandsConfig


class EconomicsConfig(BaseModel):
    contribution_margin_rate: float = Field(ge=0, le=1)
    human_review_unit_cost_paise: int = Field(ge=0)
    included_optional_costs_paise_per_case: dict[str, int] = Field(default_factory=dict)
    excluded_costs: list[str] = Field(min_length=1)

    @field_validator("included_optional_costs_paise_per_case")
    @classmethod
    def require_non_negative_optional_costs(cls, value: dict[str, int]) -> dict[str, int]:
        if any(cost < 0 for cost in value.values()):
            raise ValueError("included optional costs cannot be negative")
        return value


class UncertaintyConfig(BaseModel):
    confidence_level: float = Field(gt=0, lt=1)
    interval_method: Literal["empirical_percentile_across_seeds"]


class MeasurementConfig(BaseModel):
    attribution: AttributionConfig
    holdout: HoldoutConfig
    economics: EconomicsConfig
    uncertainty: UncertaintyConfig


class PolicyConfig(BaseModel):
    actions: list[ActionConfig]
    templates: list[TemplateConfig]
    guardrails: GuardrailConfig
    priors: PriorsConfig
    models: ModelsConfig
    tier1_rules: list[DiagnosisRuleConfig]
    receivable_rules: list[ReceivableRuleConfig]
    policy_defaults: PolicyDefaultsConfig
    ladders: list[LadderConfig]


def _load_yaml(path: Path) -> object:
    with path.open(encoding="utf-8") as file:
        return yaml.safe_load(file)


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_policy_config(config_dir: str | None = None) -> PolicyConfig:
    root = Path(config_dir) if config_dir else get_settings().config_dir
    return PolicyConfig(
        actions=_load_yaml(root / "actions.yaml"),
        templates=_load_yaml(root / "templates.yaml"),
        guardrails=_load_yaml(root / "guardrails.yaml"),
        priors=_load_yaml(root / "priors.yaml"),
        models=_load_yaml(root / "models.yaml"),
        tier1_rules=_load_yaml(root / "tier1_rules.yaml"),
        receivable_rules=_load_yaml(root / "receivable_rules.yaml"),
        policy_defaults=_load_yaml(root / "policy.yaml"),
        ladders=_load_yaml(root / "ladders.yaml"),
    )


@lru_cache
def get_measurement_config(config_dir: str | None = None) -> MeasurementConfig:
    root = Path(config_dir) if config_dir else get_settings().config_dir
    return MeasurementConfig.model_validate(_load_yaml(root / "measurement.yaml"))
