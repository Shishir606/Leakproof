from __future__ import annotations

from functools import lru_cache

from leakproof.config import get_settings
from leakproof.demo.rate_limit import InMemoryRateLimiter, RedisRateLimiter
from leakproof.providers.contracts import (
    CaseInsightProvider,
    CohortAnalysisProvider,
    EmailProvider,
    PaymentProvider,
    ProviderError,
)
from leakproof.providers.fakes import (
    FakeCaseInsightProvider,
    FakeEmailProvider,
    FakePaymentProvider,
)
from leakproof.providers.openai import (
    OpenAICaseInsightProvider,
    OpenAICohortAnalysisProvider,
)
from leakproof.providers.razorpay import RazorpayPaymentProvider
from leakproof.providers.resend import ResendEmailProvider


@lru_cache
def get_payment_provider() -> PaymentProvider:
    settings = get_settings()
    if settings.mode == "live_demo":
        return RazorpayPaymentProvider(settings.razorpay_key_id, settings.razorpay_key_secret)
    return FakePaymentProvider()


class _UnavailableCaseInsightProvider:
    def explain_case(self, _request):
        raise ProviderError(
            provider="openai",
            operation="case_insight",
            error_class="configuration_missing",
            retryable=False,
            message="OpenAI API key is not configured",
        )


@lru_cache
def get_case_insight_provider() -> CaseInsightProvider:
    settings = get_settings()
    if settings.mode == "simulation":
        return FakeCaseInsightProvider()
    if not settings.openai_api_key:
        return _UnavailableCaseInsightProvider()
    return OpenAICaseInsightProvider(
        settings.openai_api_key,
        model=settings.openai_model,
        usd_to_inr=settings.openai_usd_to_inr,
    )


class _UnavailableCohortAnalysisProvider:
    def analyze_cohort(self, _request):
        raise ProviderError(
            provider="openai",
            operation="cohort_analysis",
            error_class="configuration_missing",
            retryable=False,
            message="OpenAI cohort analysis is not configured",
        )


@lru_cache
def get_cohort_analysis_provider() -> CohortAnalysisProvider:
    settings = get_settings()
    if settings.mode == "simulation":
        # Kept out of the live branch deliberately: production must never masquerade a
        # deterministic rule engine as a model response.
        from leakproof.diagnosis.tier2 import DeterministicCohortProvider

        return DeterministicCohortProvider()
    if not settings.openai_api_key or not settings.luna_enabled:
        return _UnavailableCohortAnalysisProvider()
    return OpenAICohortAnalysisProvider(
        settings.openai_api_key,
        model=settings.openai_model,
        usd_to_inr=settings.openai_usd_to_inr,
    )


class _UnavailableEmailProvider:
    def send_recovery_email(self, _request):
        raise ProviderError(
            provider="resend",
            operation="send_recovery_email",
            error_class="configuration_missing",
            retryable=False,
            message="Resend API key or sender address is not configured",
        )


@lru_cache
def get_email_provider() -> EmailProvider:
    settings = get_settings()
    if settings.mode == "simulation":
        return FakeEmailProvider()
    if not settings.resend_api_key or not settings.resend_from_email:
        return _UnavailableEmailProvider()
    return ResendEmailProvider(settings.resend_api_key, settings.resend_from_email)


@lru_cache
def get_demo_rate_limiter() -> RedisRateLimiter | InMemoryRateLimiter:
    settings = get_settings()
    if settings.mode == "live_demo":
        return RedisRateLimiter(settings.redis_url)
    return InMemoryRateLimiter()
