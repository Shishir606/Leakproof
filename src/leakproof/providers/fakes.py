from __future__ import annotations

from dataclasses import dataclass, field

from leakproof.demo.contracts import CaseInsight
from leakproof.providers.contracts import (
    CaseInsightRequest,
    CaseInsightResult,
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
    order_idempotency: dict[str, str] = field(default_factory=dict)
    failure: ProviderError | None = None

    def create_order(self, request: CreateOrderRequest) -> PaymentOrder:
        self.create_calls.append(request)
        if self.failure:
            raise self.failure
        existing_id = self.order_idempotency.get(request.idempotency_key)
        if existing_id is not None:
            return self.orders[existing_id]
        order_id = f"order_fake_{len(self.orders) + 1}"
        order = PaymentOrder(
            order_id,
            request.amount_paise,
            request.currency,
            "created",
            request_id=f"req_order_fake_{len(self.orders) + 1}",
        )
        self.orders[order_id] = order
        self.order_idempotency[request.idempotency_key] = order_id
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
    result: CaseInsight | None = None
    calls: list[CaseInsightRequest] = field(default_factory=list)
    failure: ProviderError | None = None

    def explain_case(self, request: CaseInsightRequest) -> CaseInsightResult:
        self.calls.append(request)
        if self.failure:
            raise self.failure
        insight = self.result or CaseInsight(
            summary=(
                "The payment needs another customer-authorized attempt "
                f"({request.failure_class})."
            ),
            probable_cause=f"Tier 1 classified the case as {request.failure_class}.",
            evidence=[f"Amount band: {request.amount_band}"],
            recommended_next_step="Reopen Checkout and let the customer choose how to pay.",
            confidence=0.7,
        )
        return CaseInsightResult(
            insight=insight,
            request_id=f"resp_fake_{len(self.calls)}",
            input_tokens=40,
            output_tokens=80,
            cost_paise=1,
            latency_ms=1,
        )
