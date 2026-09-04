from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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
from leakproof.demo.contracts import DemoSessionState
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
        UniqueConstraint("id", "merchant_id", name="uq_cases_id_merchant"),
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
    merchant_id: Mapped[str | None] = mapped_column(ForeignKey("merchants.id"))
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id"))
    batch_run_id: Mapped[str | None] = mapped_column(ForeignKey("batch_runs.id"))
    purpose: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String(40), default="unknown", nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(255))
    error_class: Mapped[str | None] = mapped_column(String(100))
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


class PaymentAttemptObservation(Base):
    """Sanitized, deduplicated provider truth used for cohort analysis."""

    __tablename__ = "payment_attempt_observations"
    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "provider",
            "namespace",
            "attempt_key",
            name="uq_payment_attempt_provider_key",
        ),
        Index(
            "ix_payment_attempt_merchant_observed",
            "merchant_id",
            "namespace",
            "observed_at",
        ),
        Index("ix_payment_attempt_outcome", "merchant_id", "namespace", "outcome"),
        Index("ix_payment_attempt_issuer", "merchant_id", "namespace", "issuer"),
        Index("ix_payment_attempt_method", "merchant_id", "namespace", "method"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    namespace: Mapped[str] = mapped_column(String(160), nullable=False, default="live")
    attempt_key: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_event_key: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_payment_id: Mapped[str | None] = mapped_column(String(255))
    provider_order_id: Mapped[str | None] = mapped_column(String(255))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    method: Mapped[str] = mapped_column(String(80), nullable=False, default="unknown")
    issuer: Mapped[str] = mapped_column(String(120), nullable=False, default="unknown")
    bin_bucket: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    checkout_step: Mapped[str] = mapped_column(String(120), nullable=False, default="unknown")
    checkout_version: Mapped[str] = mapped_column(String(80), nullable=False, default="unknown")
    error_reason: Mapped[str] = mapped_column(String(160), nullable=False, default="unknown")
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DemoSession(Base):
    __tablename__ = "demo_sessions"
    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "provider_mode",
            "razorpay_order_id",
            name="uq_demo_sessions_razorpay_order",
        ),
        UniqueConstraint("id", "merchant_id", "provider_mode", name="uq_demo_session_scope"),
        CheckConstraint(
            "primary_entity_type IN ('order', 'invoice', 'subscription')",
            name="ck_demo_primary_type",
        ),
        CheckConstraint("provider_mode IN ('test', 'live')", name="ck_demo_provider_mode"),
        CheckConstraint(
            "setup_state IN ('CREATING', 'READY', 'ACTION_REQUIRED', 'FAILED', 'EXPIRED')",
            name="ck_demo_setup_state",
        ),
        CheckConstraint(
            "scenario_type IN ('PAYMENT_FAILURE','CHECKOUT_ABANDON','INVOICE_OVERDUE',"
            "'SUBSCRIPTION_HALT')",
            name="ck_demo_scenario",
        ),
        CheckConstraint(
            "primary_entity_type != 'order' OR "
            "(razorpay_order_id IS NOT NULL AND razorpay_order_id=primary_entity_id)",
            name="ck_demo_order_compatibility",
        ),
        Index("ix_demo_sessions_merchant_state", "merchant_id", "state"),
        Index("ix_demo_sessions_expires", "expires_at"),
        Index("ix_demo_sessions_recipient_hash", "recipient_hash"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), nullable=False)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False)
    razorpay_order_id: Mapped[str | None] = mapped_column(String)
    scenario_type: Mapped[str] = mapped_column(String, default="PAYMENT_FAILURE", nullable=False)
    primary_entity_type: Mapped[str] = mapped_column(String, default="order", nullable=False)
    primary_entity_id: Mapped[str] = mapped_column(
        String,
        default=lambda ctx: ctx.get_current_parameters().get("razorpay_order_id"),
        nullable=False,
    )
    provider_mode: Mapped[str] = mapped_column(String, default="test", nullable=False)
    setup_state: Mapped[str] = mapped_column(String, default="READY", nullable=False)
    capability_evidence: Mapped[str] = mapped_column(
        String, default="ARCHITECTURE_READY", nullable=False
    )
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    state: Mapped[str] = mapped_column(
        SAEnum(DemoSessionState, name="demo_session_state"),
        default=DemoSessionState.CREATED,
        nullable=False,
    )
    recipient_ciphertext: Mapped[str | None] = mapped_column(Text)
    recipient_hash: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class CheckoutEvent(Base):
    __tablename__ = "checkout_events"
    __table_args__ = (
        UniqueConstraint("session_id", "client_event_id", name="uq_checkout_events_session_client"),
        Index("ix_checkout_events_session_received", "session_id", "received_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("demo_sessions.id"), nullable=False)
    client_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON_TYPE, default=dict, nullable=False
    )


class ProviderCall(Base):
    __tablename__ = "provider_calls"
    __table_args__ = (
        Index("ix_provider_calls_session", "session_id", "created_at"),
        Index("ix_provider_calls_case", "case_id", "created_at"),
        Index("ix_provider_calls_provider_status", "provider", "status"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("demo_sessions.id"))
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id"))
    action_id: Mapped[str | None] = mapped_column(ForeignKey("actions.id"))
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    operation: Mapped[str] = mapped_column(String(80), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(255))
    safe_response_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE, default=dict, nullable=False
    )
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_number: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    error_class: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EmailDelivery(Base):
    __tablename__ = "email_deliveries"
    __table_args__ = (
        UniqueConstraint("action_id", name="uq_email_deliveries_action"),
        UniqueConstraint("case_id", name="uq_email_deliveries_case"),
        UniqueConstraint("provider_email_id", name="uq_email_deliveries_provider_email"),
        Index("ix_email_deliveries_recipient_created", "recipient_hash", "created_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("demo_sessions.id"), nullable=False)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), nullable=False)
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id"), nullable=False)
    provider_email_id: Mapped[str | None] = mapped_column(String(128))
    recipient_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class EmailDeliveryEvent(Base):
    __tablename__ = "email_delivery_events"
    __table_args__ = (
        UniqueConstraint(
            "provider_email_id", "provider_event_id", name="uq_email_delivery_provider_event"
        ),
        Index("ix_email_delivery_events_email_created", "provider_email_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    provider_email_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    safe_payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CaseInsightRecord(Base):
    __tablename__ = "case_insights"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_case_insight_confidence"),
    )

    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), primary_key=True)
    summary: Mapped[str | None] = mapped_column(String(500))
    probable_cause: Mapped[str | None] = mapped_column(String(500))
    evidence: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    recommended_next_step: Mapped[str | None] = mapped_column(String(500))
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    fallback_reason: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ProviderObligation(Base):
    """One receivable and counted case owner across sessions and event surfaces."""

    __tablename__ = "provider_obligations"
    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "provider",
            "mode",
            "entity_type",
            "provider_entity_id",
            name="uq_obligation_identity",
        ),
        UniqueConstraint("id", "merchant_id", "provider", "mode", name="uq_obligation_scope"),
        UniqueConstraint("case_id", name="uq_obligation_case"),
        ForeignKeyConstraint(
            ["case_id", "merchant_id"],
            ["cases.id", "cases.merchant_id"],
            name="fk_obligation_case_scope",
        ),
        CheckConstraint("entity_type IN ('order', 'invoice')", name="ck_obligation_type"),
        CheckConstraint("mode IN ('test', 'live')", name="ck_obligation_mode"),
        CheckConstraint(
            "recovered_paise >= 0 AND baseline_paid_paise >= 0 AND "
            "(detected_due_paise IS NULL OR recovered_paise <= detected_due_paise)",
            name="ck_obligation_credit",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    provider_entity_id: Mapped[str] = mapped_column(String, nullable=False)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    baseline_paid_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    detected_due_paise: Mapped[int | None] = mapped_column(BigInteger)
    outstanding_paise: Mapped[int | None] = mapped_column(BigInteger)
    recovered_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciliation_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    alias_of: Mapped[str | None] = mapped_column(ForeignKey("provider_obligations.id"))


class ProviderEntity(Base):
    __tablename__ = "provider_entities"
    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "provider",
            "mode",
            "entity_type",
            "provider_entity_id",
            name="uq_provider_entity_identity",
        ),
        ForeignKeyConstraint(
            ["session_id", "merchant_id", "mode"],
            ["demo_sessions.id", "demo_sessions.merchant_id", "demo_sessions.provider_mode"],
            name="fk_provider_entity_session",
        ),
        ForeignKeyConstraint(
            ["obligation_id", "merchant_id", "provider", "mode"],
            [
                "provider_obligations.id",
                "provider_obligations.merchant_id",
                "provider_obligations.provider",
                "provider_obligations.mode",
            ],
            name="fk_provider_entity_obligation",
        ),
        CheckConstraint(
            "entity_type IN ('order', 'invoice', 'subscription', 'payment', 'token')",
            name="ck_provider_entity_type",
        ),
        Index("ix_provider_entities_session", "session_id"),
        Index(
            "ix_provider_entities_root",
            "merchant_id",
            "provider",
            "mode",
            "root_entity_type",
            "root_entity_id",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    provider_entity_id: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    root_entity_type: Mapped[str | None] = mapped_column(String)
    root_entity_id: Mapped[str | None] = mapped_column(String)
    obligation_id: Mapped[str | None] = mapped_column(String)
    status: Mapped[str | None] = mapped_column(String)
    state_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safe_metadata: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)


class Settlement(Base):
    """Unique captured payment; surface events are observations, never new credit."""

    __tablename__ = "provider_settlements"
    __table_args__ = (
        UniqueConstraint(
            "merchant_id", "provider", "mode", "payment_id", name="uq_provider_settlement_payment"
        ),
        ForeignKeyConstraint(
            ["obligation_id", "merchant_id", "provider", "mode"],
            [
                "provider_obligations.id",
                "provider_obligations.merchant_id",
                "provider_obligations.provider",
                "provider_obligations.mode",
            ],
            name="fk_settlement_obligation",
        ),
        CheckConstraint(
            "amount_paise >= 0 AND credited_paise >= 0 AND credited_paise <= amount_paise",
            name="ck_settlement_credit",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    merchant_id: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    payment_id: Mapped[str] = mapped_column(String, nullable=False)
    obligation_id: Mapped[str] = mapped_column(String, nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credited_paise: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    credited_action_id: Mapped[str | None] = mapped_column(ForeignKey("actions.id"))
    organic: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
