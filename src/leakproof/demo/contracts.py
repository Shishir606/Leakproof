from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from leakproof.models.domain import LeakType
from leakproof.models.resources import SetupState
from leakproof.provenance import DataProvenance


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DemoSessionState(StrEnum):
    CREATED = "CREATED"
    CHECKOUT_OPEN = "CHECKOUT_OPEN"
    AT_RISK = "AT_RISK"
    RECOVERED = "RECOVERED"
    EXPIRED = "EXPIRED"


ALLOWED_SESSION_TRANSITIONS: dict[DemoSessionState, frozenset[DemoSessionState]] = {
    DemoSessionState.CREATED: frozenset(
        {
            DemoSessionState.CHECKOUT_OPEN,
            DemoSessionState.AT_RISK,
            DemoSessionState.RECOVERED,
            DemoSessionState.EXPIRED,
        }
    ),
    DemoSessionState.CHECKOUT_OPEN: frozenset(
        {
            DemoSessionState.AT_RISK,
            DemoSessionState.RECOVERED,
            DemoSessionState.EXPIRED,
        }
    ),
    DemoSessionState.AT_RISK: frozenset(
        {
            DemoSessionState.CHECKOUT_OPEN,
            DemoSessionState.RECOVERED,
            DemoSessionState.EXPIRED,
        }
    ),
    DemoSessionState.RECOVERED: frozenset(),
    DemoSessionState.EXPIRED: frozenset(),
}


def assert_session_transition(current: DemoSessionState, target: DemoSessionState) -> None:
    """Reject state regressions while allowing an idempotent replay."""
    if current == target:
        return
    if target not in ALLOWED_SESSION_TRANSITIONS[current]:
        raise ValueError(f"invalid demo-session transition: {current.value} -> {target.value}")


def live_case_dedupe_key(session_id: str, razorpay_order_id: str) -> str:
    """One live case per session/order, regardless of which risk signal wins."""
    if not session_id or not razorpay_order_id:
        raise ValueError("session_id and razorpay_order_id are required")
    return f"live:{session_id}:{razorpay_order_id}"


class CheckoutEventType(StrEnum):
    CHECKOUT_OPENED = "checkout_opened"
    PAYMENT_ATTEMPT_STARTED = "payment_attempt_started"
    CHECKOUT_DISMISSED = "checkout_dismissed"
    CHECKOUT_COMPLETED = "checkout_completed"


class EmailMode(StrEnum):
    ALLOWLISTED = "allowlisted"
    PREVIEW_ONLY = "preview_only"


class DemoSessionCreateRequest(StrictContract):
    scenario_type: LeakType = LeakType.PAYMENT_FAILURE
    recipient: str | None = Field(default=None, min_length=3, max_length=254)

    @field_validator("recipient")
    @classmethod
    def normalize_recipient(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().casefold()
        if normalized.count("@") != 1 or normalized.startswith("@") or normalized.endswith("@"):
            raise ValueError("recipient must be a valid email address")
        return normalized


class DemoSessionCreated(StrictContract):
    scenario_type: Literal[LeakType.PAYMENT_FAILURE, LeakType.CHECKOUT_ABANDON] = (
        LeakType.PAYMENT_FAILURE
    )
    primary_entity_type: Literal["order"] = "order"
    setup_state: SetupState = SetupState.READY
    session_id: str
    session_token: str
    razorpay_key_id: str
    razorpay_order_id: str
    amount_paise: int = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    expires_at: datetime
    email_mode: EmailMode


class CheckoutEventMetadata(StrictContract):
    attempt_id: str | None = Field(default=None, min_length=1, max_length=128)
    dismissed_by: Literal["customer", "browser", "unknown"] | None = None
    sdk_version: str | None = Field(default=None, min_length=1, max_length=32)


class CheckoutEventRequest(StrictContract):
    client_event_id: str = Field(min_length=1, max_length=128)
    event_type: CheckoutEventType
    occurred_at: datetime
    metadata: CheckoutEventMetadata = Field(default_factory=CheckoutEventMetadata)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(UTC)


class CheckoutEventReceipt(StrictContract):
    accepted: bool = True
    duplicate: bool
    event_id: int


class CheckoutPaymentVerificationRequest(StrictContract):
    razorpay_payment_id: str = Field(min_length=5, max_length=128, pattern=r"^pay_[A-Za-z0-9_]+$")
    razorpay_order_id: str = Field(min_length=7, max_length=128, pattern=r"^order_[A-Za-z0-9_]+$")
    razorpay_signature: str = Field(min_length=64, max_length=64, pattern=r"^[a-fA-F0-9]{64}$")


class CheckoutPaymentVerificationReceipt(StrictContract):
    verified: Literal[True] = True
    duplicate: bool
    state: Literal[DemoSessionState.RECOVERED] = DemoSessionState.RECOVERED
    payment_status: Literal["captured"] = "captured"


class RecoveryBootstrap(StrictContract):
    purpose: Literal["order_checkout"] = "order_checkout"
    session_id: str
    razorpay_key_id: str
    razorpay_order_id: str
    amount_paise: int = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    expires_at: datetime


class CaseInsight(StrictContract):
    summary: str = Field(min_length=1, max_length=500)
    probable_cause: str = Field(min_length=1, max_length=500)
    evidence: list[str] = Field(min_length=1, max_length=8)
    recommended_next_step: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)

    @field_validator("evidence")
    @classmethod
    def bound_evidence_items(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 300 for item in value):
            raise ValueError("evidence items must contain 1 to 300 characters")
        return value


class CaseProjection(StrictContract):
    case_id: str
    leak_type: LeakType
    state: str
    deterministic_diagnosis: dict[str, Any] | None = None
    insight: CaseInsight | None = None
    insight_status: Literal["pending", "succeeded", "fallback"] = "pending"


class ProviderStatus(StrictContract):
    provider: Literal["razorpay", "openai", "resend"]
    operation: str
    status: str
    request_id: str | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    attempts: int | None = Field(default=None, ge=1)
    error_class: str | None = None


class RecoveryActionProjection(StrictContract):
    action_id: str | None = None
    action_type: Literal[
        "recovery_link",
        "invoice_payment_link",
        "subscription_method_update",
        "email_link",
        "merchant_review",
    ]
    status: str
    scheduled_for: datetime
    executed_at: datetime | None = None
    gate_verdict: str | None = None
    provider_receipt_id: str | None = None


class TimelineItem(StrictContract):
    kind: str
    source: Literal["browser", "razorpay", "openai", "resend", "leakproof"]
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class OperationalMetrics(StrictContract):
    cases_detected: int = Field(ge=0)
    recovered_cases: int = Field(ge=0)
    recovered_amount_paise: int = Field(ge=0)
    recovery_rate: float = Field(ge=0, le=1)
    median_recovery_time_seconds: float | None = Field(default=None, ge=0)
    provider_failures: int = Field(ge=0)
    luna_cost_paise: int = Field(ge=0)


class AbandonmentCheck(StrictContract):
    status: Literal[
        "idle",
        "waiting",
        "provider_recheck",
        "provider_retry",
        "provider_pending",
        "confirmed",
        "payment_failure",
        "recovered",
    ] = "idle"
    due_at: datetime | None = None
    browser_dismissed_at: datetime | None = None
    unpaid_confirmed: bool = False


class InvoiceProjection(StrictContract):
    provider_status: str
    business_due_at: datetime
    business_overdue: bool
    aging_bucket: Literal["not_due", "under_1_day", "1_to_7_days", "over_7_days"]
    provider_expires_at: datetime | None
    detected_balance_paise: int | None
    outstanding_balance_paise: int = Field(ge=0)
    amount_paid_paise: int = Field(ge=0)
    recovered_paise: int = Field(ge=0)
    disposition: Literal["payable", "merchant_review", "paid", "provider_retry"]
    last_checked_at: datetime | None
    partial_payment: bool


class SubscriptionProjection(StrictContract):
    provider_status: str
    payment_method: str | None = None
    cycle_resolved: bool
    cycle_status: str | None = None
    detected_balance_paise: int | None = None
    outstanding_balance_paise: int = Field(default=0, ge=0)
    recovered_paise: int = Field(default=0, ge=0)
    retry_owner: Literal["razorpay"] = "razorpay"
    retry_count: int = Field(default=0, ge=0)
    method_update_available: bool = False
    authorization_repaired: bool = False
    disposition: Literal[
        "authorization_required",
        "provider_retry",
        "method_update",
        "active_with_arrears",
        "merchant_review",
        "paid",
    ]
    last_checked_at: datetime | None = None


class DemoSessionProjection(StrictContract):
    invoice: InvoiceProjection | None = None
    subscription: SubscriptionProjection | None = None
    abandonment_check: AbandonmentCheck = Field(default_factory=AbandonmentCheck)
    scenario_type: LeakType = LeakType.PAYMENT_FAILURE
    primary_entity_type: Literal["order", "invoice", "subscription"] = "order"
    setup_state: SetupState = SetupState.READY
    capability_evidence: DataProvenance = DataProvenance.ARCHITECTURE_READY
    data_provenance: DataProvenance
    session_id: str
    state: DemoSessionState
    amount_paise: int = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    expires_at: datetime
    email_mode: EmailMode
    case: CaseProjection | None = None
    recovery_url_available: bool = False
    recovery_path: str | None = Field(default=None, pattern=r"^/recover/[A-Za-z0-9_.-]+$")
    gate_verdict: str | None = None
    recovery_actions: list[RecoveryActionProjection] = Field(default_factory=list)
    provider_statuses: list[ProviderStatus] = Field(default_factory=list)
    timeline: list[TimelineItem] = Field(default_factory=list)
    end_to_end_latency_seconds: float | None = Field(default=None, ge=0)
    metrics: OperationalMetrics
    environment_metrics: OperationalMetrics


class AcceptanceSessionSummary(StrictContract):
    scenario_type: LeakType | None = None
    state: DemoSessionState
    amount_paise: int = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    email_mode: EmailMode


class AcceptanceCaseSummary(StrictContract):
    leak_type: Literal[
        "PAYMENT_FAILURE",
        "CHECKOUT_ABANDON",
        "INVOICE_OVERDUE",
        "SUBSCRIPTION_HALT",
    ]
    state: str
    deterministic_diagnosis_ready: bool
    insight_status: Literal["pending", "succeeded", "fallback"]


class AcceptanceProviderStatus(StrictContract):
    provider: Literal["razorpay", "openai", "resend"]
    operation: str
    status: str
    latency_ms: int | None = Field(default=None, ge=0)
    attempts: int | None = Field(default=None, ge=1)
    error_class: str | None = None


class AcceptanceCheck(StrictContract):
    check: str
    passed: bool
    severity: Literal["blocking", "advisory"]
    detail: str


class DemoAcceptanceExport(StrictContract):
    """Sanitized, credential-free evidence captured during the release rehearsal."""

    invoice: InvoiceProjection | None = None
    subscription: SubscriptionProjection | None = None
    schema_version: Literal["2026-09-04"] = "2026-09-04"
    data_provenance: DataProvenance
    exported_at: datetime
    passed: bool
    session: AcceptanceSessionSummary
    case: AcceptanceCaseSummary | None = None
    operational_metrics: OperationalMetrics
    provider_statuses: list[AcceptanceProviderStatus] = Field(default_factory=list)
    timeline: list[TimelineItem] = Field(default_factory=list)
    checks: list[AcceptanceCheck] = Field(default_factory=list)


class APIErrorDetail(StrictContract):
    code: str = Field(pattern=r"^[a-z0-9_]+$")
    message: str
    retryable: bool = False


class APIError(StrictContract):
    error: APIErrorDetail


class RazorpayWebhookEnvelope(BaseModel):
    """Stable outer contract; provider-specific entity bodies remain forward compatible."""

    model_config = ConfigDict(extra="allow")

    event: str = Field(min_length=1, max_length=100)
    created_at: int = Field(ge=0)
    payload: dict[str, Any]


class ResendWebhookData(BaseModel):
    model_config = ConfigDict(extra="allow")

    email_id: str = Field(min_length=1, max_length=128)
    created_at: datetime | None = None


class ResendWebhookEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal[
        "email.sent",
        "email.delivered",
        "email.bounced",
        "email.complained",
        "email.clicked",
        "email.failed",
    ]
    created_at: datetime
    data: ResendWebhookData


class InvoiceRecoveryBootstrap(StrictContract):
    purpose: Literal["invoice_hosted_payment"] = "invoice_hosted_payment"
    session_id: str
    disposition: Literal["payable", "merchant_review", "paid"] = "payable"
    redirect_url: str | None = Field(default=None, pattern=r"^https://")
    amount_due_paise: int = Field(default=0, ge=0)
    currency: str = "INR"
    expires_at: datetime


class SubscriptionRecoveryBootstrap(StrictContract):
    purpose: Literal["subscription_method_update"] = "subscription_method_update"
    session_id: str
    razorpay_key_id: str
    subscription_id: str = Field(pattern=r"^sub_[A-Za-z0-9_]+$")
    subscription_card_change: Literal[True] = True
    outstanding_invoice_id_present: bool = True
    expires_at: datetime


# Reserved contracts for later adapters; the existing route still returns only order_checkout.
ResourceRecoveryBootstrap = Annotated[
    RecoveryBootstrap | InvoiceRecoveryBootstrap | SubscriptionRecoveryBootstrap,
    Field(discriminator="purpose"),
]


class ScenarioCapability(StrictContract):
    scenario_type: LeakType
    primary_entity_type: Literal["order", "invoice", "subscription"]
    enabled: bool
    capability_evidence: DataProvenance
    reason: str | None = None


SCENARIO_CAPABILITIES = (
    ScenarioCapability(
        scenario_type=LeakType.PAYMENT_FAILURE,
        primary_entity_type="order",
        enabled=True,
        capability_evidence=DataProvenance.LIVE_PROVIDER_VERIFIED,
    ),
    ScenarioCapability(
        scenario_type=LeakType.CHECKOUT_ABANDON,
        primary_entity_type="order",
        enabled=True,
        capability_evidence=DataProvenance.LIVE_TELEMETRY_PROVIDER_RECONCILED,
        reason="Browser dismissal and the original unpaid order were provider-reconciled.",
    ),
    ScenarioCapability(
        scenario_type=LeakType.INVOICE_OVERDUE,
        primary_entity_type="invoice",
        enabled=True,
        capability_evidence=DataProvenance.CONTRACT_VERIFIED,
        reason="Requires a configured test customer and human hosted partial/full payment.",
    ),
    ScenarioCapability(
        scenario_type=LeakType.SUBSCRIPTION_HALT,
        primary_entity_type="subscription",
        enabled=True,
        capability_evidence=DataProvenance.CONTRACT_VERIFIED,
        reason=(
            "Requires a configured reusable test plan and human Razorpay "
            "authorization/failure controls."
        ),
    ),
)


class InvoiceSessionCreated(StrictContract):
    scenario_type: Literal[LeakType.INVOICE_OVERDUE] = LeakType.INVOICE_OVERDUE
    primary_entity_type: Literal["invoice"] = "invoice"
    primary_entity_id: str = Field(pattern=r"^inv_[A-Za-z0-9_]+$")
    session_id: str
    session_token: str
    setup_state: SetupState
    amount_paise: int = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    expires_at: datetime
    email_mode: EmailMode


class SubscriptionSessionCreated(StrictContract):
    scenario_type: Literal[LeakType.SUBSCRIPTION_HALT]
    primary_entity_type: Literal["subscription"] = "subscription"
    primary_entity_id: str = Field(pattern=r"^sub_[A-Za-z0-9_]+$")
    session_id: str
    session_token: str
    setup_state: SetupState
    amount_paise: int = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    expires_at: datetime
    email_mode: EmailMode
    authorization_url: str | None = Field(default=None, pattern=r"^https://")


ResourceSessionCreated = Annotated[
    DemoSessionCreated | InvoiceSessionCreated | SubscriptionSessionCreated,
    Field(discriminator="primary_entity_type"),
]
