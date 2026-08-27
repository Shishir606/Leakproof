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


def poll_checkout_abandonment() -> PollResult:
    return PollResult("checkout_abandonment", scanned=0, signals=0)


def poll_invoice_aging() -> PollResult:
    return PollResult("invoice_aging", scanned=0, signals=0)


def poll_subscription_health() -> PollResult:
    return PollResult("subscription_health", scanned=0, signals=0)


def reconcile_provider_events() -> PollResult:
    return PollResult("provider_reconciler_24h", scanned=0, signals=0)
