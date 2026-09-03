from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx2
from pydantic import ValidationError

from leakproof.models.resources import EntityRef
from leakproof.providers.contracts import (
    CreateInvoiceRequest,
    CreateOrderRequest,
    CreateSubscriptionRequest,
    Invoice,
    Payment,
    PaymentOrder,
    ProviderError,
    Subscription,
)


class RazorpayPaymentProvider:
    """Small Razorpay Orders/Payments adapter with bounded transport retries."""

    def __init__(
        self,
        key_id: str,
        key_secret: str,
        *,
        base_url: str = "https://api.razorpay.com",
        timeout_seconds: float = 5.0,
        max_attempts: int = 2,
        client: httpx2.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not key_id or not key_secret:
            raise ValueError("Razorpay credentials are required")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._client = client or httpx2.Client(
            base_url=base_url,
            auth=httpx2.BasicAuth(key_id, key_secret),
            timeout=timeout_seconds,
            trust_env=False,
            headers={"Accept": "application/json"},
        )
        self._key_id = key_id
        self._max_attempts = max_attempts
        self._sleep = sleep

    @staticmethod
    def _request_id(response: httpx2.Response) -> str | None:
        return response.headers.get("x-razorpay-request-id") or response.headers.get("x-request-id")

    def _request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        last_error: ProviderError | None = None
        attempt_limit = kwargs.pop("_attempt_limit", self._max_attempts)
        for attempt in range(1, attempt_limit + 1):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx2.RequestError as exc:
                last_error = ProviderError(
                    provider="razorpay",
                    operation=f"{method} {path}",
                    error_class="transport_error",
                    retryable=True,
                    message="Razorpay request failed",
                )
                if attempt < attempt_limit:
                    self._sleep(0.1 * attempt)
                    continue
                raise last_error from exc

            request_id = self._request_id(response)
            if response.status_code in {401, 403}:
                raise ProviderError(
                    provider="razorpay",
                    operation=f"{method} {path}",
                    error_class="authentication_failed",
                    retryable=False,
                    message="Razorpay authentication failed",
                    request_id=request_id,
                    status_code=response.status_code,
                )
            if response.status_code == 429 or response.status_code >= 500:
                last_error = ProviderError(
                    provider="razorpay",
                    operation=f"{method} {path}",
                    error_class=(
                        "rate_limited" if response.status_code == 429 else "provider_unavailable"
                    ),
                    retryable=True,
                    message="Razorpay is temporarily unavailable",
                    request_id=request_id,
                    status_code=response.status_code,
                )
                if attempt < attempt_limit:
                    self._sleep(0.1 * attempt)
                    continue
                raise last_error
            if response.status_code >= 400:
                raise ProviderError(
                    provider="razorpay",
                    operation=f"{method} {path}",
                    error_class="request_rejected",
                    retryable=False,
                    message="Razorpay rejected the request",
                    request_id=request_id,
                    status_code=response.status_code,
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise ProviderError(
                    provider="razorpay",
                    operation=f"{method} {path}",
                    error_class="malformed_response",
                    retryable=False,
                    message="Razorpay returned malformed JSON",
                    request_id=request_id,
                    status_code=response.status_code,
                ) from exc
            if not isinstance(payload, dict):
                raise ProviderError(
                    provider="razorpay",
                    operation=f"{method} {path}",
                    error_class="malformed_response",
                    retryable=False,
                    message="Razorpay returned an unexpected response shape",
                    request_id=request_id,
                    status_code=response.status_code,
                )
            payload["__request_id"] = request_id
            return payload
        assert last_error is not None
        raise last_error

    @staticmethod
    def _required(payload: dict[str, Any], key: str, expected: type) -> Any:
        value = payload.get(key)
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            raise ProviderError(
                provider="razorpay",
                operation="decode_response",
                error_class="malformed_response",
                retryable=False,
                message=f"Razorpay response is missing {key}",
                request_id=payload.get("__request_id"),
            )
        return value

    def create_order(self, request: CreateOrderRequest) -> PaymentOrder:
        payload = self._request_json(
            "POST",
            "/v1/orders",
            json={
                "amount": request.amount_paise,
                "currency": request.currency,
                "receipt": request.receipt,
                "notes": request.notes,
            },
        )
        order = PaymentOrder(
            id=self._required(payload, "id", str),
            amount_paise=self._required(payload, "amount", int),
            currency=self._required(payload, "currency", str),
            status=self._required(payload, "status", str),
            request_id=payload.get("__request_id"),
        )
        if order.amount_paise != request.amount_paise or order.currency != request.currency:
            raise ProviderError(
                provider="razorpay",
                operation="create_order",
                error_class="response_mismatch",
                retryable=False,
                message="Razorpay order amount or currency did not match the request",
                request_id=order.request_id,
            )
        return order

    def fetch_payment(self, payment_id: str) -> Payment:
        payload = self._request_json("GET", f"/v1/payments/{quote(payment_id, safe='')}")
        return self._decode_payment(payload)

    def list_order_payments(self, order_id: str) -> list[Payment]:
        payload = self._request_json("GET", f"/v1/orders/{quote(order_id, safe='')}/payments")
        items = payload.get("items")
        if not isinstance(items, list):
            raise ProviderError(
                provider="razorpay",
                operation="list_order_payments",
                error_class="malformed_response",
                retryable=False,
                message="Razorpay payment collection is missing items",
                request_id=payload.get("__request_id"),
            )
        return [self._decode_payment(item, payload.get("__request_id")) for item in items]

    def _decode_payment(self, payload: Any, collection_request_id: str | None = None) -> Payment:
        if not isinstance(payload, dict):
            raise ProviderError(
                provider="razorpay",
                operation="decode_payment",
                error_class="malformed_response",
                retryable=False,
                message="Razorpay returned an invalid payment",
                request_id=collection_request_id,
            )
        request_id = payload.get("__request_id") or collection_request_id
        return Payment(
            id=self._required(payload, "id", str),
            order_id=self._required(payload, "order_id", str),
            amount_paise=self._required(payload, "amount", int),
            currency=self._required(payload, "currency", str),
            status=self._required(payload, "status", str),
            method=payload.get("method") if isinstance(payload.get("method"), str) else None,
            request_id=request_id,
            created_at=payload.get("created_at"),
            invoice_id=payload.get("invoice_id"),
        )

    def _decode_invoice(self, payload: Any, request_id: str | None = None) -> Invoice:
        if not isinstance(payload, dict):
            raise self._decode_error("invoice", request_id)
        try:
            amount = self._required(payload, "amount", int)
            status = self._required(payload, "status", str)
            # Razorpay leaves both balance fields null until a draft is issued.
            # The draft still has a verified total, so its only valid balance is
            # zero paid and the full amount outstanding. Other states remain
            # strict because inferring their balance could falsely close a case.
            amount_paid = payload.get("amount_paid")
            amount_due = payload.get("amount_due")
            if status == "draft" and amount_paid is None and amount_due is None:
                amount_paid, amount_due = 0, amount
            return Invoice(
                request_id=payload.get("__request_id") or request_id,
                id=self._required(payload, "id", str),
                order_id=payload.get("order_id"),
                subscription_id=payload.get("subscription_id"),
                status=status,
                amount_paise=amount,
                amount_paid_paise=amount_paid,
                amount_due_paise=amount_due,
                currency=self._required(payload, "currency", str),
                short_url=payload.get("short_url"),
                customer_id=payload.get("customer_id"),
                issued_at=payload.get("issued_at"),
                expire_by=payload.get("expire_by"),
                partial_payment=payload.get("partial_payment", False),
            )
        except ValidationError as exc:
            raise self._decode_error("invoice", payload.get("__request_id") or request_id) from exc

    def _decode_subscription(self, payload: Any, request_id: str | None = None) -> Subscription:
        if not isinstance(payload, dict):
            raise self._decode_error("subscription", request_id)
        try:
            return Subscription(
                request_id=payload.get("__request_id") or request_id,
                id=self._required(payload, "id", str),
                plan_id=self._required(payload, "plan_id", str),
                status=self._required(payload, "status", str),
                payment_method=(
                    payload.get("payment_method")
                    if isinstance(payload.get("payment_method"), str)
                    else None
                ),
                short_url=payload.get("short_url"),
                current_start=payload.get("current_start"),
                current_end=payload.get("current_end"),
                charge_at=payload.get("charge_at"),
                paid_count=payload.get("paid_count", 0),
                remaining_count=payload.get("remaining_count", 0),
            )
        except ValidationError as exc:
            raise self._decode_error(
                "subscription", payload.get("__request_id") or request_id
            ) from exc

    @staticmethod
    def _decode_error(resource: str, request_id: str | None) -> ProviderError:
        return ProviderError(
            provider="razorpay",
            operation=f"decode_{resource}",
            error_class="malformed_response",
            retryable=False,
            message=f"Razorpay returned an invalid {resource}",
            request_id=request_id,
        )

    @staticmethod
    def _items(payload: dict[str, Any], resource: str) -> list[Any]:
        items = payload.get("items")
        if not isinstance(items, list):
            raise RazorpayPaymentProvider._decode_error(resource, payload.get("__request_id"))
        return items

    def _require_test_setup(self) -> None:
        if not self._key_id.startswith("rzp_test_"):
            raise ProviderError(
                provider="razorpay",
                operation="resource_setup",
                error_class="live_mode_not_supported",
                retryable=False,
                message="New resource setup is test-mode only",
            )

    def create_invoice(self, request: CreateInvoiceRequest) -> Invoice:
        self._require_test_setup()
        payload = self._request_json(
            "POST",
            "/v1/invoices",
            _attempt_limit=1,
            json={
                "type": "invoice",
                "draft": "1",
                "currency": request.currency,
                "customer_id": request.customer_id,
                "receipt": request.receipt,
                "partial_payment": request.partial_payment,
                **({"expire_by": request.expire_by} if request.expire_by is not None else {}),
                "sms_notify": False,
                "email_notify": False,
                "line_items": [
                    {
                        "name": request.line_item_name,
                        "amount": request.amount_paise,
                        "currency": request.currency,
                        "quantity": 1,
                    }
                ],
                "notes": {"idempotency_key": request.idempotency_key},
            },
        )
        invoice = self._decode_invoice(payload)
        if invoice.amount_paise != request.amount_paise or invoice.currency != request.currency:
            raise ProviderError(
                provider="razorpay",
                operation="create_invoice",
                error_class="response_mismatch",
                retryable=False,
                message="Razorpay invoice amount or currency did not match the request",
            )
        return invoice

    def _match_id(self, resource, expected: str):
        if resource.id != expected:
            raise ProviderError(
                provider="razorpay",
                operation="decode_resource",
                error_class="response_mismatch",
                retryable=False,
                message="Razorpay returned another resource",
            )
        return resource

    def issue_invoice(self, invoice_id: str) -> Invoice:
        self._require_test_setup()
        EntityRef(entity_type="invoice", entity_id=invoice_id)
        return self._match_id(
            self._decode_invoice(
                self._request_json("POST", f"/v1/invoices/{invoice_id}/issue", _attempt_limit=1)
            ),
            invoice_id,
        )

    def fetch_invoice(self, invoice_id: str) -> Invoice:
        EntityRef(entity_type="invoice", entity_id=invoice_id)
        return self._match_id(
            self._decode_invoice(self._request_json("GET", f"/v1/invoices/{invoice_id}")),
            invoice_id,
        )

    def _collection(self, path: str, params: dict, decode) -> list:
        collected = []
        for skip in range(0, 1000, 100):
            payload = self._request_json("GET", path, params={**params, "count": 100, "skip": skip})
            items = self._items(payload, "resource collection")
            collected.extend(decode(item, payload.get("__request_id")) for item in items)
            if len(items) < 100:
                return collected
        raise ProviderError(
            provider="razorpay",
            operation="list_resources",
            error_class="reconciliation_limit",
            retryable=False,
            message="Resource collection exceeds the bounded reconciliation limit",
        )

    def list_invoices(self, *, subscription_id: str | None = None) -> list[Invoice]:
        if subscription_id:
            EntityRef(entity_type="subscription", entity_id=subscription_id)
        invoices = self._collection(
            "/v1/invoices",
            {"subscription_id": subscription_id} if subscription_id else {},
            self._decode_invoice,
        )
        if subscription_id and any(item.subscription_id != subscription_id for item in invoices):
            raise ProviderError(
                provider="razorpay",
                operation="list_subscription_invoices",
                error_class="response_mismatch",
                retryable=False,
                message="Razorpay returned an invoice for another subscription",
            )
        return invoices

    def create_subscription(self, request: CreateSubscriptionRequest) -> Subscription:
        self._require_test_setup()
        subscription = self._decode_subscription(
            self._request_json(
                "POST",
                "/v1/subscriptions",
                _attempt_limit=1,
                json={
                    "plan_id": request.plan_id,
                    "total_count": request.total_count,
                    "customer_notify": False,
                    "notes": request.notes,
                },
            )
        )
        if subscription.plan_id != request.plan_id:
            raise ProviderError(
                provider="razorpay",
                operation="create_subscription",
                error_class="response_mismatch",
                retryable=False,
                message="Razorpay returned a subscription for another plan",
            )
        return subscription

    def fetch_subscription(self, subscription_id: str) -> Subscription:
        EntityRef(entity_type="subscription", entity_id=subscription_id)
        return self._match_id(
            self._decode_subscription(
                self._request_json("GET", f"/v1/subscriptions/{subscription_id}")
            ),
            subscription_id,
        )

    def list_subscriptions(self) -> list[Subscription]:
        return self._collection("/v1/subscriptions", {}, self._decode_subscription)

    def list_subscription_invoices(self, subscription_id: str) -> list[Invoice]:
        return self.list_invoices(subscription_id=subscription_id)


RazorpayProvider = RazorpayPaymentProvider
