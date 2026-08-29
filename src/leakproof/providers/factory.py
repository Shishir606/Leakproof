from __future__ import annotations

from functools import lru_cache

from leakproof.config import get_settings
from leakproof.demo.rate_limit import InMemoryRateLimiter, RedisRateLimiter
from leakproof.providers.contracts import CaseInsightProvider, PaymentProvider, ProviderError
from leakproof.providers.fakes import FakeCaseInsightProvider, FakePaymentProvider
from leakproof.providers.openai import OpenAICaseInsightProvider
from leakproof.providers.razorpay import RazorpayPaymentProvider


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


@lru_cache
def get_demo_rate_limiter() -> RedisRateLimiter | InMemoryRateLimiter:
    settings = get_settings()
    if settings.mode == "live_demo":
        return RedisRateLimiter(settings.redis_url)
    return InMemoryRateLimiter()
