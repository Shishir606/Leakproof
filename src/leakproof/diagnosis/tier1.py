from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from leakproof.audit.timeline import append_event
from leakproof.config import (
    DiagnosisRuleConfig,
    ReceivableRuleConfig,
    get_policy_config,
)
from leakproof.models.db import Diagnosis, Event, RecoveryCase
from leakproof.models.domain import CaseState, LeakType


class _Rule(Protocol):
    id: str
    match: dict[str, Any]
    failure_class: str
    confidence: float


class DiagnosisResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_id: str
    failure_class: str
    confidence: float
    evidence: dict[str, Any]
    customer_contact_allowed: bool | None = None
    retry_allowed: bool | None = None
    retry_strategy: str | None = None
    max_contacts: int | None = None
    escalate_to_tier2: bool = False


def _matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    for key, wanted in expected.items():
        value = actual.get(key)
        if isinstance(wanted, list):
            if value not in wanted:
                return False
        elif value != wanted:
            return False
    return True


def _first_match(facts: dict[str, Any], rules: Iterable[_Rule]) -> _Rule:
    for rule in rules:
        if _matches(facts, rule.match):
            return rule
    raise ValueError("diagnosis rules must end with a fallback rule")


def classify_payment_failure(
    evidence: dict[str, Any], rules: list[DiagnosisRuleConfig] | None = None
) -> DiagnosisResult:
    rule = _first_match(evidence, rules or get_policy_config().tier1_rules)
    assert isinstance(rule, DiagnosisRuleConfig)
    return DiagnosisResult(
        rule_id=rule.id,
        failure_class=rule.failure_class,
        confidence=rule.confidence,
        evidence=dict(evidence),
        customer_contact_allowed=rule.customer_contact_allowed,
        retry_allowed=rule.retry_allowed,
        retry_strategy=rule.retry_strategy,
        max_contacts=rule.max_contacts,
        escalate_to_tier2=rule.escalate_to_tier2,
    )


def _aging_bucket(days_overdue: int) -> str:
    if days_overdue <= 7:
        return "1-7"
    if days_overdue <= 30:
        return "8-30"
    return "31-90"


def _payer_history(evidence: dict[str, Any]) -> str:
    behavior = evidence.get("payer_behavior")
    if behavior in {"SLOW_BUT_GOOD", "USUALLY_ON_TIME"}:
        return "GOOD"
    if behavior == "CHRONIC_LATE":
        return "STRESSED"
    if behavior == "HIGH_RISK":
        return "RISKY"
    rate = float(evidence.get("historical_payment_rate", 0))
    if rate >= 0.9:
        return "GOOD"
    if rate >= 0.7:
        return "STRESSED"
    return "RISKY"


def _invoice_size(amount_paise: int) -> str:
    if amount_paise <= 5_000_000:  # up to INR 50,000
        return "SMALL"
    if amount_paise <= 50_000_000:  # up to INR 5,00,000
        return "MEDIUM"
    return "LARGE"


def classify_receivable(
    evidence: dict[str, Any],
    amount_paise: int,
    rules: list[ReceivableRuleConfig] | None = None,
) -> DiagnosisResult:
    facts = {
        **evidence,
        "aging_bucket": evidence.get("aging_bucket")
        or _aging_bucket(int(evidence.get("days_overdue", 0))),
        "payer_history": _payer_history(evidence),
        "invoice_size": _invoice_size(amount_paise),
    }
    rule = _first_match(facts, rules or get_policy_config().receivable_rules)
    return DiagnosisResult(
        rule_id=rule.id,
        failure_class=rule.failure_class,
        confidence=rule.confidence,
        evidence=facts,
    )


def diagnose_case(session: Session, case_id: str) -> Diagnosis:
    """Persist one deterministic diagnosis and its append-only audit event."""
    case = session.get(RecoveryCase, case_id)
    if case is None:
        raise LookupError(case_id)
    existing = session.get(Diagnosis, case_id)
    if existing is not None:
        return existing

    source_event = session.scalar(
        select(Event)
        .where(Event.case_id == case_id, Event.kind.in_(["DETECTED", "SIGNAL"]))
        .order_by(Event.seq.desc())
    )
    if source_event is None:
        raise ValueError(f"case {case_id} has no signal evidence")
    evidence = dict(source_event.payload.get("evidence", {}))
    if case.leak_type == LeakType.INVOICE_OVERDUE.value:
        result = classify_receivable(evidence, case.amount_at_risk)
    else:
        result = classify_payment_failure(evidence)

    diagnosis = Diagnosis(
        case_id=case.id,
        tier=1,
        failure_class=result.failure_class,
        confidence=result.confidence,
        evidence=result.evidence,
        rule_id=result.rule_id,
    )
    session.add(diagnosis)
    append_event(
        session,
        case,
        kind="DIAGNOSED",
        payload=result.model_dump(mode="json"),
        actor="tier1",
    )
    case.state = CaseState.DIAGNOSED.value
    session.flush()
    return diagnosis
