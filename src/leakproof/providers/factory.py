from __future__ import annotations

from functools import lru_cache

from leakproof.config import get_settings
from leakproof.demo.rate_limit import InMemoryRateLimiter, RedisRateLimiter
from leakproof.providers.contracts import PaymentProvider
from leakproof.providers.fakes import FakePaymentProvider
from leakproof.providers.razorpay import RazorpayPaymentProvider


@lru_cache
def get_payment_provider() -> PaymentProvider:
    settings = get_settings()
    if settings.mode == "live_demo":
        return RazorpayPaymentProvider(settings.razorpay_key_id, settings.razorpay_key_secret)
    return FakePaymentProvider()


@lru_cache
def get_demo_rate_limiter() -> RedisRateLimiter | InMemoryRateLimiter:
    settings = get_settings()
    if settings.mode == "live_demo":
        return RedisRateLimiter(settings.redis_url)
    return InMemoryRateLimiter()
