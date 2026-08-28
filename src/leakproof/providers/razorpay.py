from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx2

from leakproof.providers.contracts import (
    CreateOrderRequest,
    Payment,
    PaymentOrder,
    ProviderError,
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
        self._max_attempts = max_attempts
        self._sleep = sleep

    @staticmethod
    def _request_id(response: httpx2.Response) -> str | None:
        return response.headers.get("x-razorpay-request-id") or response.headers.get(
            "x-request-id"
        )

    def _request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        last_error: ProviderError | None = None
        for attempt in range(1, self._max_attempts + 1):
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
                if attempt < self._max_attempts:
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
                if attempt < self._max_attempts:
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
        if not isinstance(value, expected):
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
        payload = self._request_json(
            "GET", f"/v1/orders/{quote(order_id, safe='')}/payments"
        )
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

    def _decode_payment(
        self, payload: Any, collection_request_id: str | None = None
    ) -> Payment:
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
        )
