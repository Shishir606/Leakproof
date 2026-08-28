"""Provider-neutral integration boundaries."""

from leakproof.providers.contracts import (
    CaseInsightProvider,
    CaseInsightRequest,
    CreateOrderRequest,
    EmailProvider,
    EmailSendRequest,
    EmailSendResult,
    Payment,
    PaymentOrder,
    PaymentProvider,
    ProviderError,
)

__all__ = [
    "CaseInsightProvider",
    "CaseInsightRequest",
    "CreateOrderRequest",
    "EmailProvider",
    "EmailSendRequest",
    "EmailSendResult",
    "Payment",
    "PaymentOrder",
    "PaymentProvider",
    "ProviderError",
]
