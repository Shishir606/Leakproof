"""Scheduled sensor entry points.

Provider read adapters land with the simulator/integrations. Keeping these tasks explicit on
foundation day makes cadence and ownership visible without fabricating upstream records.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PollResult:
    sensor: str
    scanned: int
    signals: int
    failed: int = 0


def poll_checkout_abandonment() -> PollResult:
    return PollResult("checkout_abandonment", scanned=0, signals=0)


def poll_invoice_aging() -> PollResult:
    from leakproof.config import get_settings
    from leakproof.db import SessionLocal
    from leakproof.demo.invoices import reconcile_invoice_sessions
    from leakproof.providers.factory import get_payment_provider

    return PollResult(
        **reconcile_invoice_sessions(
            session_factory=SessionLocal, provider=get_payment_provider(), settings=get_settings()
        )
    )


def poll_subscription_health() -> PollResult:
    from leakproof.config import get_settings
    from leakproof.db import SessionLocal
    from leakproof.demo.subscriptions import reconcile_subscription_sessions
    from leakproof.providers.factory import get_payment_provider

    return PollResult(
        **reconcile_subscription_sessions(
            session_factory=SessionLocal, provider=get_payment_provider(), settings=get_settings()
        )
    )


def reconcile_provider_events() -> PollResult:
    return PollResult("provider_reconciler_24h", scanned=0, signals=0)
