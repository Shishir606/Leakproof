from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from leakproof.db import Base
from leakproof.models.domain import Arm, CaseOutcome, CaseState, LeakType

BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")
JSON_TYPE = JSON().with_variant(JSONB, "postgresql")


def utcnow() -> datetime:
    return datetime.now().astimezone()


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    policy: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), nullable=False)
    segment: Mapped[str | None] = mapped_column(String)
    locale: Mapped[str] = mapped_column(String, default="en-IN", nullable=False)
    protected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dnc: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dnc_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Consent(Base):
    __tablename__ = "consents"
    __table_args__ = (UniqueConstraint("customer_id", "channel"),)

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False)
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    basis: Mapped[str] = mapped_column(String, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RecoveryCase(Base):
    __tablename__ = "cases"
    __table_args__ = (
        UniqueConstraint("merchant_id", "dedupe_key", name="uq_cases_merchant_dedupe"),
        Index("ix_cases_merchant_state", "merchant_id", "state"),
        Index("ix_cases_customer", "customer_id"),
        Index("ix_cases_batch_run", "batch_run_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), nullable=False)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False)
    leak_type: Mapped[str] = mapped_column(SAEnum(LeakType, name="leak_type"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[str] = mapped_column(String, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String, nullable=False)
    batch_run_id: Mapped[str | None] = mapped_column(String)
    amount_band: Mapped[str] = mapped_column(String, default="UNASSIGNED", nullable=False)
    amount_at_risk: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String, default="INR", nullable=False)
    state: Mapped[str] = mapped_column(
        SAEnum(CaseState, name="case_state"), default=CaseState.DETECTED, nullable=False
    )
    arm: Mapped[str] = mapped_column(SAEnum(Arm, name="arm"), nullable=False)
    outcome: Mapped[str | None] = mapped_column(SAEnum(CaseOutcome, name="case_outcome"))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attribution_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    events: Mapped[list[Event]] = relationship(
        back_populates="case", order_by="Event.seq", cascade="all, delete-orphan"
    )


class BatchRun(Base):
    __tablename__ = "batch_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    holdout_seed: Mapped[int] = mapped_column(Integer, nullable=False)
    holdout_fraction: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    measurement_config: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("case_id", "seq", name="uq_events_case_seq"),)

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    case: Mapped[RecoveryCase] = relationship(back_populates="events")


@event.listens_for(Event, "before_update")
def reject_event_update(_mapper: Any, _connection: Any, _event: Event) -> None:
    raise ValueError("case events are append-only and cannot be updated")


@event.listens_for(Event, "before_delete")
def reject_event_delete(_mapper: Any, _connection: Any, _event: Event) -> None:
    raise ValueError("case events are append-only and cannot be deleted")


class Diagnosis(Base):
    __tablename__ = "diagnoses"

    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), primary_key=True)
    tier: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    failure_class: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    rule_id: Mapped[str | None] = mapped_column(String)
    diagnosed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Action(Base):
    __tablename__ = "actions"
    __table_args__ = (
        UniqueConstraint("case_id", "step_index", name="uq_actions_case_step"),
        UniqueConstraint("idempotency_key", name="uq_actions_idempotency_key"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), nullable=False)
    step_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    action_type: Mapped[str] = mapped_column(String, nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verdict: Mapped[str | None] = mapped_column(String)
    verdict_rules: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str | None] = mapped_column(String)
    provider_ref: Mapped[str | None] = mapped_column(String)
    status: Mapped[str | None] = mapped_column(String)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    ev_estimate: Mapped[int | None] = mapped_column(BigInteger)


class ActuatorReceipt(Base):
    """A simulated provider's durable idempotency ledger."""

    __tablename__ = "actuator_receipts"

    idempotency_key: Mapped[str] = mapped_column(String, primary_key=True)
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    provider_ref: Mapped[str] = mapped_column(String, nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (Index("ix_contacts_customer_sent", "customer_id", "sent_at"),)

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RecoveryAttribution(Base):
    __tablename__ = "recovery_attributions"
    __table_args__ = (UniqueConstraint("case_id", name="uq_attribution_case"),)

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), nullable=False)
    payment_entity_id: Mapped[str] = mapped_column(String, nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    matched_by: Mapped[str] = mapped_column(String, nullable=False)
    credit_rule: Mapped[str] = mapped_column(String, nullable=False)
    credited_action_id: Mapped[str | None] = mapped_column(ForeignKey("actions.id"))
    credited_action_type: Mapped[str | None] = mapped_column(String)
    touch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    organic: Mapped[bool] = mapped_column(Boolean, nullable=False)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attributed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Suppression(Base):
    __tablename__ = "suppressions"
    __table_args__ = (Index("ix_suppressions_merchant_expires", "merchant_id", "expires_at"),)

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    merchant_id: Mapped[str] = mapped_column(String, nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    pattern: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    opened_by: Mapped[str] = mapped_column(String, nullable=False)


class Promise(Base):
    __tablename__ = "promises"
    __table_args__ = (
        UniqueConstraint("case_id", "transcript_ref", name="uq_promises_case_transcript"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), nullable=False)
    promised_on: Mapped[date] = mapped_column(Date, nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    captured_via: Mapped[str] = mapped_column(String, nullable=False)
    kept: Mapped[bool | None] = mapped_column(Boolean)
    transcript_ref: Mapped[str | None] = mapped_column(String)


class VoiceTurn(Base):
    """One provider-delivered customer turn in a bounded voice conversation."""

    __tablename__ = "voice_turns"
    __table_args__ = (
        UniqueConstraint("action_id", "turn_number", name="uq_voice_turn_action_number"),
    )

    provider_turn_id: Mapped[str] = mapped_column(String, primary_key=True)
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id"), nullable=False)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), nullable=False)
    turn_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    transcript: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = mapped_column(String, nullable=False)
    reply_template_id: Mapped[str] = mapped_column(String, nullable=False)
    ended: Mapped[bool] = mapped_column(Boolean, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id"))
    purpose: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    retries: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    called_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    suite: Mapped[str] = mapped_column(String, nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String)
    model: Mapped[str | None] = mapped_column(String)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    ran_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint(
            "merchant_id", "provider", "provider_event_key", name="uq_webhook_provider_event"
        ),
        Index("ix_webhooks_unprocessed", "processed_at", "received_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    merchant_id: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    provider_event_key: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    signature_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
