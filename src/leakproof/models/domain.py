from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


class LeakType(StrEnum):
    PAYMENT_FAILURE = "PAYMENT_FAILURE"
    CHECKOUT_ABANDON = "CHECKOUT_ABANDON"
    SUBSCRIPTION_HALT = "SUBSCRIPTION_HALT"
    INVOICE_OVERDUE = "INVOICE_OVERDUE"


class CaseState(StrEnum):
    DETECTED = "DETECTED"
    DIAGNOSED = "DIAGNOSED"
    PLANNED = "PLANNED"
    ACTING = "ACTING"
    WAITING = "WAITING"
    VERIFYING = "VERIFYING"
    CLOSED = "CLOSED"
    SUPPRESSED = "SUPPRESSED"
    STOPPED = "STOPPED"
    ESCALATED = "ESCALATED"


class Arm(StrEnum):
    TREATMENT = "TREATMENT"
    HOLDOUT = "HOLDOUT"


class CaseOutcome(StrEnum):
    RECOVERED = "RECOVERED"
    LOST = "LOST"
    ABANDONED = "ABANDONED"
    HUMAN = "HUMAN"
    SUPPRESSED = "SUPPRESSED"


class CaseEvent(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    kind: str
    payload: dict[str, Any]
    actor: str
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class CaseSnapshot(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    merchant_id: str
    customer_id: str
    leak_type: LeakType
    entity_type: str
    entity_id: str
    dedupe_key: str
    batch_run_id: str | None = None
    amount_band: str = "UNASSIGNED"
    amount_at_risk: int
    currency: str
    state: CaseState
    arm: Arm
    detected_at: datetime
    attribution_until: datetime

    @field_validator("detected_at", "attribution_until")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class ReplayedCase(BaseModel):
    case: CaseSnapshot
    events: list[CaseEvent]
    replayed_state: CaseState
    projection_matches: bool
