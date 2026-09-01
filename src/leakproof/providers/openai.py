from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx2
from pydantic import ValidationError

from leakproof.demo.contracts import CaseInsight
from leakproof.providers.contracts import (
    CaseInsightRequest,
    CaseInsightResult,
    CohortAnalysisRequest,
    CohortAnalysisResult,
    ProviderError,
)

CASE_INSIGHT_PROMPT_VERSION = "case_insight_v1"
_INPUT_USD_PER_MILLION = Decimal("0.20")
_OUTPUT_USD_PER_MILLION = Decimal("1.20")


def _cost_paise(input_tokens: int, output_tokens: int, usd_to_inr: Decimal) -> int:
    usd = (
        Decimal(input_tokens) * _INPUT_USD_PER_MILLION
        + Decimal(output_tokens) * _OUTPUT_USD_PER_MILLION
    ) / Decimal(1_000_000)
    return max(0, math.ceil(usd * usd_to_inr * Decimal(100)))


def _output_text(payload: dict[str, Any]) -> str:
    for item in payload.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str) and text:
                    return text
    raise ValueError("Responses payload did not contain output text")


class OpenAICaseInsightProvider:
    """Responses API adapter with bounded retries and strict CaseInsight decoding."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gpt-5.6-luna",
        base_url: str = "https://api.openai.com",
        timeout_seconds: float = 8.0,
        max_attempts: int = 2,
        max_output_tokens: int = 600,
        usd_to_inr: Decimal | float | str = Decimal("100"),
        client: httpx2.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAI API key is required")
        if model != "gpt-5.6-luna":
            raise ValueError("the live case-insight adapter is pinned to gpt-5.6-luna")
        if max_attempts not in {1, 2}:
            raise ValueError("case insights support one initial attempt and one retry")
        self._client = client or httpx2.Client(
            base_url=base_url,
            timeout=timeout_seconds,
            trust_env=False,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        self._model = model
        self._max_attempts = max_attempts
        self._max_output_tokens = max_output_tokens
        self._usd_to_inr = Decimal(str(usd_to_inr))
        self._sleep = sleep

    @staticmethod
    def _request_payload(request: CaseInsightRequest, model: str, max_tokens: int) -> dict:
        input_payload = {
            "failure_class": request.failure_class,
            "payment_method": request.payment_method,
            "amount_band": request.amount_band,
            "aggregate_provider_fields": request.aggregate_provider_fields,
        }
        return {
            "model": model,
            "instructions": (
                "Explain the payment recovery case using only the supplied classifications. "
                "Do not infer identity, contact details, exact identifiers, approval, or consent. "
                "The deterministic Tier 1 classification remains authoritative."
            ),
            "input": json.dumps(input_payload, sort_keys=True, separators=(",", ":")),
            "reasoning": {"effort": "low"},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "case_insight",
                    "strict": True,
                    "schema": CaseInsight.model_json_schema(),
                }
            },
            "tools": [],
            "store": False,
            "max_output_tokens": max_tokens,
        }

    def explain_case(self, request: CaseInsightRequest) -> CaseInsightResult:
        body = self._request_payload(request, self._model, self._max_output_tokens)
        started = time.perf_counter()
        total_input = 0
        total_output = 0
        last_request_id: str | None = None
        last_error_class = "provider_unavailable"
        last_status: int | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._client.post("/v1/responses", json=body)
            except httpx2.TimeoutException as exc:
                last_error_class = "timeout"
                if attempt < self._max_attempts:
                    self._sleep(0.1 * attempt)
                    continue
                raise ProviderError(
                    "openai",
                    "case_insight",
                    last_error_class,
                    True,
                    "OpenAI case insight timed out",
                    input_tokens=total_input,
                    output_tokens=total_output,
                    cost_paise=_cost_paise(total_input, total_output, self._usd_to_inr),
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    attempts=attempt,
                ) from exc
            except httpx2.RequestError as exc:
                last_error_class = "transport_error"
                if attempt < self._max_attempts:
                    self._sleep(0.1 * attempt)
                    continue
                raise ProviderError(
                    "openai",
                    "case_insight",
                    last_error_class,
                    True,
                    "OpenAI case insight request failed",
                    input_tokens=total_input,
                    output_tokens=total_output,
                    cost_paise=_cost_paise(total_input, total_output, self._usd_to_inr),
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    attempts=attempt,
                ) from exc

            last_status = response.status_code
            last_request_id = response.headers.get("x-request-id")
            if response.status_code in {401, 403}:
                raise ProviderError(
                    "openai",
                    "case_insight",
                    "authentication_failed",
                    False,
                    "OpenAI authentication failed",
                    request_id=last_request_id,
                    status_code=response.status_code,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    attempts=attempt,
                )
            if response.status_code == 429 or response.status_code >= 500:
                last_error_class = (
                    "quota_exhausted" if response.status_code == 429 else "provider_unavailable"
                )
                if attempt < self._max_attempts:
                    self._sleep(0.1 * attempt)
                    continue
                break
            if response.status_code >= 400:
                raise ProviderError(
                    "openai",
                    "case_insight",
                    "request_rejected",
                    False,
                    "OpenAI rejected the case-insight request",
                    request_id=last_request_id,
                    status_code=response.status_code,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    attempts=attempt,
                )

            try:
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("status") != "completed":
                    raise ValueError("response was not completed")
                usage = payload.get("usage") or {}
                input_tokens = int(usage.get("input_tokens", 0))
                output_tokens = int(usage.get("output_tokens", 0))
                total_input += max(0, input_tokens)
                total_output += max(0, output_tokens)
                last_request_id = str(payload.get("id") or last_request_id or "") or None
                insight = CaseInsight.model_validate_json(_output_text(payload))
            except (TypeError, ValueError, ValidationError):
                last_error_class = "invalid_schema"
                if attempt < self._max_attempts:
                    self._sleep(0.1 * attempt)
                    continue
                break
            latency_ms = round((time.perf_counter() - started) * 1000)
            return CaseInsightResult(
                insight=insight,
                request_id=last_request_id,
                input_tokens=total_input,
                output_tokens=total_output,
                cost_paise=_cost_paise(total_input, total_output, self._usd_to_inr),
                latency_ms=latency_ms,
                attempts=attempt,
            )

        raise ProviderError(
            "openai",
            "case_insight",
            last_error_class,
            last_error_class in {"timeout", "transport_error", "provider_unavailable"},
            "OpenAI case insight was unavailable or invalid",
            request_id=last_request_id,
            status_code=last_status,
            input_tokens=total_input,
            output_tokens=total_output,
            cost_paise=_cost_paise(total_input, total_output, self._usd_to_inr),
            latency_ms=round((time.perf_counter() - started) * 1000),
            attempts=self._max_attempts,
        )


class OpenAICohortAnalysisProvider:
    """Strict, tool-free Responses adapter for consequential cohort proposals."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gpt-5.6-luna",
        base_url: str = "https://api.openai.com",
        timeout_seconds: float = 8.0,
        max_attempts: int = 2,
        usd_to_inr: Decimal | float | str = Decimal("100"),
        client: httpx2.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAI API key is required")
        if max_attempts not in {1, 2}:
            raise ValueError("cohort analysis supports at most two attempts")
        self._client = client or httpx2.Client(
            base_url=base_url,
            timeout=timeout_seconds,
            trust_env=False,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        self._model = model
        self._max_attempts = max_attempts
        self._usd_to_inr = Decimal(str(usd_to_inr))
        self._sleep = sleep

    def analyze_cohort(self, request: CohortAnalysisRequest) -> CohortAnalysisResult:
        body = {
            "model": self._model,
            "instructions": request.instructions,
            "input": json.dumps(request.aggregate_payload, sort_keys=True, separators=(",", ":")),
            "reasoning": {"effort": "low"},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "cohort_intervention_proposal",
                    "strict": True,
                    "schema": request.output_schema,
                }
            },
            "tools": [],
            "store": False,
            "max_output_tokens": request.max_output_tokens,
        }
        started = time.perf_counter()
        total_input = 0
        total_output = 0
        request_id: str | None = None
        error_class = "provider_unavailable"
        status_code: int | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._client.post("/v1/responses", json=body)
            except httpx2.TimeoutException as exc:
                error_class = "timeout"
                if attempt < self._max_attempts:
                    self._sleep(0.1 * attempt)
                    continue
                cause: Exception | None = exc
                break
            except httpx2.RequestError as exc:
                error_class = "transport_error"
                if attempt < self._max_attempts:
                    self._sleep(0.1 * attempt)
                    continue
                cause = exc
                break
            status_code = response.status_code
            request_id = response.headers.get("x-request-id")
            if response.status_code in {401, 403}:
                error_class = "authentication_failed"
                cause = None
                break
            if response.status_code == 429 or response.status_code >= 500:
                error_class = (
                    "quota_exhausted" if response.status_code == 429 else "provider_unavailable"
                )
                if attempt < self._max_attempts:
                    self._sleep(0.1 * attempt)
                    continue
                cause = None
                break
            if response.status_code >= 400:
                error_class = "request_rejected"
                cause = None
                break
            try:
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("status") != "completed":
                    raise ValueError("response was not completed")
                usage = payload.get("usage") or {}
                total_input += max(0, int(usage.get("input_tokens", 0)))
                total_output += max(0, int(usage.get("output_tokens", 0)))
                request_id = str(payload.get("id") or request_id or "") or None
                data = json.loads(_output_text(payload))
                if not isinstance(data, dict):
                    raise ValueError("cohort output must be an object")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                error_class = "invalid_schema"
                if attempt < self._max_attempts:
                    self._sleep(0.1 * attempt)
                    continue
                cause = exc
                break
            return CohortAnalysisResult(
                data=data,
                request_id=request_id,
                input_tokens=total_input,
                output_tokens=total_output,
                cost_paise=_cost_paise(total_input, total_output, self._usd_to_inr),
                latency_ms=round((time.perf_counter() - started) * 1000),
                attempts=attempt,
            )
        error = ProviderError(
            provider="openai",
            operation="cohort_analysis",
            error_class=error_class,
            retryable=error_class in {"timeout", "transport_error", "provider_unavailable"},
            message="OpenAI cohort analysis was unavailable or invalid",
            request_id=request_id,
            status_code=status_code,
            input_tokens=total_input,
            output_tokens=total_output,
            cost_paise=_cost_paise(total_input, total_output, self._usd_to_inr),
            latency_ms=round((time.perf_counter() - started) * 1000),
            attempts=self._max_attempts,
        )
        if cause is not None:
            raise error from cause
        raise error
