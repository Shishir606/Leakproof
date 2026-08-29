from __future__ import annotations

import time
from collections.abc import Callable

import httpx2

from leakproof.providers.contracts import EmailSendRequest, EmailSendResult, ProviderError


class ResendEmailProvider:
    """Bounded Resend adapter that always supplies the action idempotency key."""

    def __init__(
        self,
        api_key: str,
        from_email: str,
        *,
        base_url: str = "https://api.resend.com",
        timeout_seconds: float = 5.0,
        max_attempts: int = 2,
        client: httpx2.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key or not from_email:
            raise ValueError("Resend API key and sender address are required")
        if max_attempts < 1 or max_attempts > 3:
            raise ValueError("Resend max_attempts must be between one and three")
        self._client = client or httpx2.Client(
            base_url=base_url,
            timeout=timeout_seconds,
            trust_env=False,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        self._from_email = from_email
        self._max_attempts = max_attempts
        self._sleep = sleep

    def send_recovery_email(self, request: EmailSendRequest) -> EmailSendResult:
        subject = request.template_variables.get("subject")
        body = request.template_variables.get("body")
        if not subject or not body:
            raise ValueError("the registered email template must supply subject and body")

        started = time.perf_counter()
        last_error: ProviderError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._client.post(
                    "/emails",
                    headers={"Idempotency-Key": request.idempotency_key},
                    json={
                        "from": self._from_email,
                        "to": [request.recipient],
                        "subject": subject,
                        "text": body,
                    },
                )
            except httpx2.TimeoutException as exc:
                last_error = ProviderError(
                    "resend",
                    "send_recovery_email",
                    "timeout",
                    True,
                    "Resend email request timed out",
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    attempts=attempt,
                )
                if attempt < self._max_attempts:
                    self._sleep(0.1 * attempt)
                    continue
                raise last_error from exc
            except httpx2.RequestError as exc:
                last_error = ProviderError(
                    "resend",
                    "send_recovery_email",
                    "transport_error",
                    True,
                    "Resend email request failed",
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    attempts=attempt,
                )
                if attempt < self._max_attempts:
                    self._sleep(0.1 * attempt)
                    continue
                raise last_error from exc

            request_id = response.headers.get("x-request-id")
            if response.status_code in {401, 403}:
                raise ProviderError(
                    "resend",
                    "send_recovery_email",
                    "authentication_failed",
                    False,
                    "Resend authentication failed",
                    request_id=request_id,
                    status_code=response.status_code,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    attempts=attempt,
                )
            if response.status_code in {409, 429} or response.status_code >= 500:
                error_name = ""
                if response.status_code == 409:
                    try:
                        error_payload = response.json()
                        error_name = str(
                            error_payload.get("name") or error_payload.get("type") or ""
                        )
                    except (TypeError, ValueError):
                        pass
                idempotency_mismatch = error_name == "invalid_idempotent_request"
                error_class = (
                    "idempotency_mismatch"
                    if idempotency_mismatch
                    else {
                        409: "idempotency_conflict",
                        429: "rate_limited",
                    }.get(response.status_code, "provider_unavailable")
                )
                last_error = ProviderError(
                    "resend",
                    "send_recovery_email",
                    error_class,
                    not idempotency_mismatch,
                    "Resend is temporarily unavailable",
                    request_id=request_id,
                    status_code=response.status_code,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    attempts=attempt,
                )
                if not idempotency_mismatch and attempt < self._max_attempts:
                    self._sleep(0.1 * attempt)
                    continue
                raise last_error
            if response.status_code >= 400:
                raise ProviderError(
                    "resend",
                    "send_recovery_email",
                    "request_rejected",
                    False,
                    "Resend rejected the recovery email",
                    request_id=request_id,
                    status_code=response.status_code,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    attempts=attempt,
                )
            try:
                payload = response.json()
                provider_email_id = payload["id"]
                if not isinstance(provider_email_id, str) or not provider_email_id:
                    raise ValueError
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderError(
                    "resend",
                    "send_recovery_email",
                    "malformed_response",
                    False,
                    "Resend returned malformed JSON",
                    request_id=request_id,
                    status_code=response.status_code,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    attempts=attempt,
                ) from exc
            return EmailSendResult(
                provider_email_id=provider_email_id,
                status="sent",
                request_id=request_id or provider_email_id,
                latency_ms=round((time.perf_counter() - started) * 1000),
                attempts=attempt,
            )

        assert last_error is not None
        raise last_error
