from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from leakproof.demo.contracts import CaseInsight


@dataclass(frozen=True)
class ProviderError(RuntimeError):
    provider: str
    operation: str
    error_class: str
    retryable: bool
    message: str
    request_id: str | None = None
    status_code: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_paise: int = 0
    latency_ms: int = 0
    attempts: int = 1

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class CreateOrderRequest:
    amount_paise: int
    currency: str
    receipt: str
    idempotency_key: str
    notes: dict[str, str]


@dataclass(frozen=True)
class PaymentOrder:
    id: str
    amount_paise: int
    currency: str
    status: str
    request_id: str | None = None


@dataclass(frozen=True)
class Payment:
    id: str
    order_id: str
    amount_paise: int
    currency: str
    status: str
    method: str | None = None
    request_id: str | None = None
    created_at: int | None = None
    invoice_id: str | None = None


@runtime_checkable
class PaymentProvider(Protocol):
    def create_order(self, request: CreateOrderRequest) -> PaymentOrder: ...

    def fetch_payment(self, payment_id: str) -> Payment: ...

    def list_order_payments(self, order_id: str) -> list[Payment]: ...


@dataclass(frozen=True)
class EmailSendRequest:
    action_id: str
    case_id: str
    recipient: str
    template_id: str
    template_variables: dict[str, str]
    idempotency_key: str


@dataclass(frozen=True)
class EmailSendResult:
    provider_email_id: str
    status: str
    request_id: str | None = None
    latency_ms: int = 0
    attempts: int = 1


@runtime_checkable
class EmailProvider(Protocol):
    def send_recovery_email(self, request: EmailSendRequest) -> EmailSendResult: ...


@dataclass(frozen=True)
class CaseInsightRequest:
    failure_class: str
    payment_method: str | None
    amount_band: Literal["LOW", "MEDIUM", "HIGH"]
    aggregate_provider_fields: dict[str, str | int | float | bool | None]


@dataclass(frozen=True)
class CaseInsightResult:
    insight: CaseInsight
    request_id: str | None
    input_tokens: int
    output_tokens: int
    cost_paise: int
    latency_ms: int
    attempts: int = 1


@runtime_checkable
class CaseInsightProvider(Protocol):
    def explain_case(self, request: CaseInsightRequest) -> CaseInsightResult: ...


@dataclass(frozen=True)
class CohortAnalysisRequest:
    aggregate_payload: dict[str, Any]
    output_schema: dict[str, Any]
    model: str
    max_output_tokens: int
    instructions: str


@dataclass(frozen=True)
class CohortAnalysisResult:
    data: Any
    request_id: str | None
    input_tokens: int
    output_tokens: int
    cost_paise: int
    latency_ms: int
    attempts: int = 1


@runtime_checkable
class CohortAnalysisProvider(Protocol):
    def analyze_cohort(self, request: CohortAnalysisRequest) -> CohortAnalysisResult: ...


# Small boundaries for later adapters. Declaring a protocol does not enable a surface.
OrderPaymentProvider = PaymentProvider


from pydantic import Field, model_validator  # noqa: E402

from leakproof.models.resources import EntityRef, ResourceContract  # noqa: E402


class Invoice(ResourceContract):
    customer_id: str | None = Field(default=None, pattern=r"^cust_[A-Za-z0-9_]+$")
    issued_at: int | None = Field(default=None, ge=0, strict=True)
    expire_by: int | None = Field(default=None, ge=0, strict=True)
    partial_payment: bool = False
    request_id: str | None = None
    id: str = Field(pattern=r"^inv_[A-Za-z0-9_]+$")
    order_id: str | None = Field(default=None, pattern=r"^order_[A-Za-z0-9_]+$")
    subscription_id: str | None = Field(default=None, pattern=r"^sub_[A-Za-z0-9_]+$")
    status: Literal["draft", "issued", "partially_paid", "paid", "cancelled", "expired", "deleted"]
    amount_paise: int = Field(ge=0)
    amount_paid_paise: int = Field(ge=0)
    amount_due_paise: int = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    short_url: str | None = Field(default=None, pattern=r"^https://")

    @model_validator(mode="after")
    def balanced(self):
        if self.amount_paid_paise + self.amount_due_paise != self.amount_paise:
            raise ValueError("invoice balance mismatch")
        if self.status == "paid" and self.amount_due_paise:
            raise ValueError("paid invoice has an outstanding balance")
        return self


class Subscription(ResourceContract):
    request_id: str | None = None
    id: str = Field(pattern=r"^sub_[A-Za-z0-9_]+$")
    status: Literal[
        "created",
        "authenticated",
        "active",
        "pending",
        "halted",
        "cancelled",
        "completed",
        "expired",
        "paused",
    ]
    plan_id: str = Field(pattern=r"^plan_[A-Za-z0-9_]+$")
    payment_method: Literal["card", "upi", "emandate"] | None = None
    # An explicit invoice identifies the affected cycle, never paid_count.
    affected_invoice_id: str | None = Field(default=None, pattern=r"^inv_[A-Za-z0-9_]+$")


class ProviderEntityStatus(ResourceContract):
    entity: EntityRef
    status: str = Field(min_length=1, max_length=80)
    root: EntityRef | None = None


class CreateInvoiceRequest(ResourceContract):
    partial_payment: bool = True
    expire_by: int | None = Field(default=None, ge=0, strict=True)
    amount_paise: int = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    receipt: str = Field(min_length=1, max_length=40)
    customer_id: str = Field(pattern=r"^cust_[A-Za-z0-9_]+$")
    line_item_name: str = Field(min_length=1, max_length=80)
    notify_customer: Literal[False] = False
    idempotency_key: str = Field(min_length=1, max_length=160)


class CreateSubscriptionRequest(ResourceContract):
    plan_id: str = Field(pattern=r"^plan_[A-Za-z0-9_]+$")
    total_count: int = Field(gt=0)
    customer_notify: Literal[False] = False


@runtime_checkable
class InvoiceProvider(Protocol):
    def create_invoice(self, request: CreateInvoiceRequest) -> Invoice: ...

    def issue_invoice(self, invoice_id: str) -> Invoice: ...

    def fetch_invoice(self, invoice_id: str) -> Invoice: ...

    def list_invoices(self, *, subscription_id: str | None = None) -> list[Invoice]: ...


@runtime_checkable
class SubscriptionProvider(Protocol):
    def create_subscription(self, request: CreateSubscriptionRequest) -> Subscription: ...

    def fetch_subscription(self, subscription_id: str) -> Subscription: ...

    def list_subscriptions(self) -> list[Subscription]: ...

    def list_subscription_invoices(self, subscription_id: str) -> list[Invoice]: ...
