from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from leakproof.demo.contracts import CaseInsight


@dataclass(frozen=True)
class ProviderError(RuntimeError):
    provider: str
    operation: str
    error_class: str
    retryable: bool
    message: str

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


@runtime_checkable
class EmailProvider(Protocol):
    def send_recovery_email(self, request: EmailSendRequest) -> EmailSendResult: ...


@dataclass(frozen=True)
class CaseInsightRequest:
    failure_class: str
    payment_method: str | None
    amount_band: Literal["LOW", "MEDIUM", "HIGH"]
    aggregate_provider_fields: dict[str, str | int | float | bool | None]


@runtime_checkable
class CaseInsightProvider(Protocol):
    def explain_case(self, request: CaseInsightRequest) -> CaseInsight: ...
