"""Provider-neutral identities and explicit signals for every recovery surface.

A subscription is a parent; its invoice ID identifies a cycle. No customer/amount,
paid_count, or latest-invoice inference is permitted by these contracts.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from leakproof.models.domain import LeakType


class EntityType(StrEnum):
    ORDER = "order"
    INVOICE = "invoice"
    SUBSCRIPTION = "subscription"
    PAYMENT = "payment"
    TOKEN = "token"


class SetupState(StrEnum):
    CREATING = "CREATING"
    READY = "READY"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class RecoveryPurpose(StrEnum):
    ORDER_CHECKOUT = "order_checkout"
    INVOICE_HOSTED_PAYMENT = "invoice_hosted_payment"
    SUBSCRIPTION_METHOD_UPDATE = "subscription_method_update"


class ResourceContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EntityRef(ResourceContract):
    entity_type: EntityType
    entity_id: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$")

    @model_validator(mode="after")
    def validate_provider_id(self):
        prefix = {
            "order": "order_",
            "invoice": "inv_",
            "subscription": "sub_",
            "payment": "pay_",
            "token": "token_",
        }[self.entity_type]
        if not self.entity_id.startswith(prefix) or len(self.entity_id) <= len(prefix):
            raise ValueError("provider ID does not match entity type")
        return self


class ObligationRef(EntityRef):
    entity_type: Literal[EntityType.ORDER, EntityType.INVOICE]


class ProviderScope(ResourceContract):
    merchant_id: str = Field(min_length=1, max_length=255)
    provider: Literal["razorpay"] = "razorpay"
    mode: Literal["test", "live"] = "test"

    def identity(self, entity: EntityRef) -> str:
        parts = [self.merchant_id, self.provider, self.mode, entity.entity_type, entity.entity_id]
        return (
            "obl_" + hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()
        )


class SignalBase(ResourceContract):
    scope: ProviderScope
    entity: EntityRef
    root: EntityRef | None = None
    obligation: ObligationRef | None = None
    source: Literal["razorpay_webhook", "razorpay_api", "browser_provider_reconciled"]
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("signal occurrence requires a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def relationship_required(self):
        if self.entity.entity_type in {EntityType.SUBSCRIPTION, EntityType.TOKEN}:
            if self.obligation and self.obligation.entity_type != EntityType.INVOICE:
                raise ValueError("recurring obligations require an explicit invoice")
        if self.entity.entity_type == EntityType.INVOICE:
            if self.obligation and self.obligation != ObligationRef(**self.entity.model_dump()):
                raise ValueError("invoice is its own obligation")
        if self.root and self.root.entity_type == EntityType.INVOICE and self.obligation:
            if self.root.entity_id != self.obligation.entity_id:
                raise ValueError("invoice root conflicts with obligation")
        if self.entity.entity_type == EntityType.ORDER and self.obligation:
            if (
                self.obligation.entity_type == EntityType.ORDER
                and self.entity.entity_id != self.obligation.entity_id
            ):
                raise ValueError("order is its own obligation")
        return self


class RiskSignal(SignalBase):
    kind: Literal["risk"] = "risk"
    leak_type: LeakType
    customer_id: str = Field(min_length=1)
    amount_due_paise: int = Field(ge=0)
    baseline_paid_paise: int = Field(default=0, ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    mandate_evidence: Literal["qualified"] | None = None

    @model_validator(mode="after")
    def qualified_mandate(self):
        if self.leak_type == LeakType.MANDATE_BROKEN and self.mandate_evidence != "qualified":
            raise ValueError("mandate classification requires qualified provider evidence")
        return self


class MandateInvalidEvidence(ResourceContract):
    """Narrow contract for the only qualified evidence implemented by Track D.

    This deliberately models an eMandate subsequent-payment failure, rather than
    accepting provider descriptions or subscription lifecycle state as evidence.
    """

    evidence_type: Literal["emandate_subsequent_payment_failure"]
    payment_id: str = Field(pattern=r"^pay_[A-Za-z0-9_]+$")
    subscription_id: str = Field(pattern=r"^sub_[A-Za-z0-9_]+$")
    invoice_id: str = Field(pattern=r"^inv_[A-Za-z0-9_]+$")
    method: Literal["emandate"]
    recurring: Literal[True]
    error_reason: Literal["mandate_not_active"]


class EntityStateSignal(SignalBase):
    kind: Literal["state"] = "state"
    state: Literal[
        "pending",
        "halted",
        "active",
        "authorization_repaired",
        "cancelled",
        "expired",
        "partially_paid",
        "reconciliation_required",
    ]
    amount_due_paise: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def state_is_not_money(self):
        if self.amount_due_paise is not None and self.state != "partially_paid":
            raise ValueError("service and authorization state cannot change invoice balance")
        return self


class RecoverySignal(SignalBase):
    kind: Literal["recovery"] = "recovery"
    leak_type: LeakType | None = None
    payment_id: str | None = Field(default=None, pattern=r"^pay_[A-Za-z0-9_]+$")
    amount_paise: int = Field(default=0, ge=0)
    amount_due_paise: int | None = Field(default=None, ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    settlement: Literal["captured_payment", "full_settlement", "authorization_repaired"]

    @model_validator(mode="after")
    def monetary_evidence(self):
        if self.settlement == "captured_payment" and not self.payment_id:
            raise ValueError("captured settlement requires payment identity")
        if self.payment_id and not self.amount_paise:
            raise ValueError("zero-value authorization is state evidence, not settlement")
        if self.settlement == "authorization_repaired" and self.amount_due_paise is not None:
            raise ValueError("authorization repair cannot change invoice balance")
        if not self.payment_id and self.amount_paise:
            raise ValueError("cumulative amounts are not payment credit")
        if self.settlement == "authorization_repaired" and (self.payment_id or self.amount_paise):
            raise ValueError("authorization repair carries no invoice revenue")
        return self


ProviderSignal = Annotated[
    RiskSignal | EntityStateSignal | RecoverySignal, Field(discriminator="kind")
]

# Same-obligation classification only. Scenario selection never participates.
LEAK_PRECEDENCE = {
    LeakType.CHECKOUT_ABANDON: 0,
    LeakType.PAYMENT_FAILURE: 1,
    LeakType.INVOICE_OVERDUE: 2,
    LeakType.SUBSCRIPTION_HALT: 3,
    LeakType.MANDATE_BROKEN: 4,
}
