from __future__ import annotations

from dataclasses import dataclass, field

from leakproof.demo.contracts import CaseInsight
from leakproof.providers.contracts import (
    CaseInsightRequest,
    CreateOrderRequest,
    EmailSendRequest,
    EmailSendResult,
    Payment,
    PaymentOrder,
    ProviderError,
)


@dataclass
class FakePaymentProvider:
    orders: dict[str, PaymentOrder] = field(default_factory=dict)
    payments: dict[str, Payment] = field(default_factory=dict)
    create_calls: list[CreateOrderRequest] = field(default_factory=list)
    failure: ProviderError | None = None

    def create_order(self, request: CreateOrderRequest) -> PaymentOrder:
        self.create_calls.append(request)
        if self.failure:
            raise self.failure
        order_id = f"order_fake_{len(self.orders) + 1}"
        order = PaymentOrder(order_id, request.amount_paise, request.currency, "created")
        self.orders[order_id] = order
        return order

    def fetch_payment(self, payment_id: str) -> Payment:
        if self.failure:
            raise self.failure
        try:
            return self.payments[payment_id]
        except KeyError as exc:
            raise LookupError("payment not found") from exc

    def list_order_payments(self, order_id: str) -> list[Payment]:
        if self.failure:
            raise self.failure
        return [payment for payment in self.payments.values() if payment.order_id == order_id]


@dataclass
class FakeEmailProvider:
    calls: list[EmailSendRequest] = field(default_factory=list)
    failure: ProviderError | None = None

    def send_recovery_email(self, request: EmailSendRequest) -> EmailSendResult:
        self.calls.append(request)
        if self.failure:
            raise self.failure
        return EmailSendResult(
            provider_email_id=f"email_fake_{len(self.calls)}",
            status="sent",
            request_id=f"req_email_fake_{len(self.calls)}",
        )


@dataclass
class FakeCaseInsightProvider:
    result: CaseInsight
    calls: list[CaseInsightRequest] = field(default_factory=list)
    failure: ProviderError | None = None

    def explain_case(self, request: CaseInsightRequest) -> CaseInsight:
        self.calls.append(request)
        if self.failure:
            raise self.failure
        return self.result
