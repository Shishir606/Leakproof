"""Provider-neutral integration boundaries."""

from leakproof.providers.contracts import (
    CaseInsightProvider,
    CaseInsightRequest,
    CaseInsightResult,
    CreateOrderRequest,
    EmailProvider,
    EmailSendRequest,
    EmailSendResult,
    Payment,
    PaymentOrder,
    PaymentProvider,
    ProviderError,
)
from leakproof.providers.openai import OpenAICaseInsightProvider
from leakproof.providers.razorpay import RazorpayPaymentProvider
from leakproof.providers.resend import ResendEmailProvider

__all__ = [
    "CaseInsightProvider",
    "CaseInsightRequest",
    "CaseInsightResult",
    "CreateOrderRequest",
    "EmailProvider",
    "EmailSendRequest",
    "EmailSendResult",
    "Payment",
    "PaymentOrder",
    "PaymentProvider",
    "ProviderError",
    "RazorpayPaymentProvider",
    "OpenAICaseInsightProvider",
    "ResendEmailProvider",
]
