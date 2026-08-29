from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class RecoveryBootstrap(StrictContract):
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
    leak_type: Literal["PAYMENT_FAILURE", "CHECKOUT_ABANDON"]
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
    action_type: Literal["recovery_link", "email_link"]
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


class DemoSessionProjection(StrictContract):
    session_id: str
    state: DemoSessionState
    amount_paise: int = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    expires_at: datetime
    email_mode: EmailMode
    case: CaseProjection | None = None
    recovery_url_available: bool = False
    gate_verdict: str | None = None
    recovery_actions: list[RecoveryActionProjection] = Field(default_factory=list)
    provider_statuses: list[ProviderStatus] = Field(default_factory=list)
    timeline: list[TimelineItem] = Field(default_factory=list)
    end_to_end_latency_seconds: float | None = Field(default=None, ge=0)
    metrics: OperationalMetrics


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
