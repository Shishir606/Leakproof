from __future__ import annotations

from dataclasses import dataclass, field

from leakproof.demo.contracts import CaseInsight
from leakproof.providers.contracts import (
    CaseInsightRequest,
    CaseInsightResult,
    CreateInvoiceRequest,
    CreateOrderRequest,
    CreateSubscriptionRequest,
    EmailSendRequest,
    EmailSendResult,
    Invoice,
    Payment,
    PaymentOrder,
    ProviderError,
    Subscription,
)


@dataclass
class FakePaymentProvider:
    subscriptions: dict[str, Subscription] = field(default_factory=dict)
    subscription_create_calls: list[CreateSubscriptionRequest] = field(default_factory=list)
    invoices: dict[str, Invoice] = field(default_factory=dict)
    invoice_create_calls: list[CreateInvoiceRequest] = field(default_factory=list)
    invoice_issue_calls: list[str] = field(default_factory=list)
    orders: dict[str, PaymentOrder] = field(default_factory=dict)
    payments: dict[str, Payment] = field(default_factory=dict)
    create_calls: list[CreateOrderRequest] = field(default_factory=list)
    order_idempotency: dict[str, str] = field(default_factory=dict)
    failure: ProviderError | None = None

    def create_invoice(self, request: CreateInvoiceRequest) -> Invoice:
        self.invoice_create_calls.append(request)
        if self.failure:
            raise self.failure
        invoice = Invoice(
            id=f"inv_fake_{len(self.invoices) + 1}",
            status="draft",
            customer_id=request.customer_id,
            amount_paise=request.amount_paise,
            amount_paid_paise=0,
            amount_due_paise=request.amount_paise,
            currency=request.currency,
            partial_payment=request.partial_payment,
            expire_by=request.expire_by,
        )
        self.invoices[invoice.id] = invoice
        return invoice

    def issue_invoice(self, invoice_id: str) -> Invoice:
        import time

        self.invoice_issue_calls.append(invoice_id)
        invoice = self.fetch_invoice(invoice_id).model_copy(
            update={
                "status": "issued",
                "issued_at": int(time.time()),
                "order_id": f"order_{invoice_id}",
                "short_url": "https://rzp.io/i/fixture",
            }
        )
        self.invoices[invoice.id] = invoice
        return invoice

    def fetch_invoice(self, invoice_id: str) -> Invoice:
        if self.failure:
            raise self.failure
        if invoice_id not in self.invoices:
            raise ProviderError(
                "razorpay",
                "fetch_invoice",
                "not_found",
                False,
                "Invoice unavailable",
                status_code=404,
            )
        return self.invoices[invoice_id]

    def list_invoices(self, *, subscription_id: str | None = None) -> list[Invoice]:
        if self.failure:
            raise self.failure
        return [
            i
            for i in self.invoices.values()
            if subscription_id is None or i.subscription_id == subscription_id
        ]

    def create_subscription(self, request: CreateSubscriptionRequest) -> Subscription:
        self.subscription_create_calls.append(request)
        if self.failure:
            raise self.failure
        subscription = Subscription(
            id=f"sub_fake_{len(self.subscriptions) + 1}",
            plan_id=request.plan_id,
            status="created",
            short_url="https://rzp.io/i/subscription-fixture",
            remaining_count=request.total_count,
        )
        self.subscriptions[subscription.id] = subscription
        return subscription

    def fetch_subscription(self, subscription_id: str) -> Subscription:
        if self.failure:
            raise self.failure
        try:
            return self.subscriptions[subscription_id]
        except KeyError as exc:
            raise ProviderError(
                "razorpay", "fetch_subscription", "not_found", False, "Subscription unavailable"
            ) from exc

    def list_subscriptions(self) -> list[Subscription]:
        if self.failure:
            raise self.failure
        return list(self.subscriptions.values())

    def list_subscription_invoices(self, subscription_id: str) -> list[Invoice]:
        return self.list_invoices(subscription_id=subscription_id)

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
                f"The payment needs another customer-authorized attempt ({request.failure_class})."
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
