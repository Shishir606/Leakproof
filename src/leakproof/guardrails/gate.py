from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from leakproof.audit.timeline import append_event
from leakproof.config import GuardrailConfig, get_policy_config
from leakproof.messaging.templates import RenderedMessage
from leakproof.models.db import Action, RecoveryCase

Decision = Literal["ALLOW", "DENY", "DEFER_TO_HUMAN", "RESCHEDULE"]
IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class GateCase:
    merchant_id: str
    verified_paid: bool = False
    disputed: bool = False
    attempts: int = 0
    retries: int = 0


@dataclass(frozen=True)
class GateCustomer:
    dnc: bool = False
    protected: bool = False


@dataclass(frozen=True)
class GateDiagnosis:
    failure_class: str


@dataclass(frozen=True)
class GatePlan:
    max_steps: int


@dataclass(frozen=True)
class ContactRecord:
    channel: str
    sent_at: datetime


@dataclass(frozen=True)
class PlannedAction:
    action_type: str
    scheduled_for: datetime
    is_customer_facing: bool = False
    channel: str | None = None
    amount_paise: int = 0
    consent_granted: bool = False
    consent_basis: str | None = None
    rendered_message: RenderedMessage | None = None
    last_retry_at: datetime | None = None
    makes_debit: bool = False
    mandate_max_amount_paise: int | None = None
    invoice_outstanding_paise: int | None = None
    is_emandate: bool = False
    special_emandate_category: bool = False
    pre_debit_notice_at: datetime | None = None
    additional_factor_authenticated: bool = False
    human_confirmed: bool = False
    standing_merchant_approval: bool = False
    contains_penalty_language: bool = False
    contains_legal_language: bool = False
    recipient_is_customer: bool = True


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    passed: bool
    decision: Decision
    reason: str
    retry_at: datetime | None = None
    evidence: dict[str, Any] | None = None


@dataclass(frozen=True, init=False)
class GateVerdict:
    decision: Decision
    rules_evaluated: tuple[RuleResult, ...]
    reason: str | None
    retry_at: datetime | None

    @classmethod
    def _from_gate(cls, rules: list[RuleResult]) -> GateVerdict:
        instance = object.__new__(cls)
        denials = [rule for rule in rules if not rule.passed and rule.decision == "DENY"]
        deferrals = [
            rule for rule in rules if not rule.passed and rule.decision == "DEFER_TO_HUMAN"
        ]
        reschedules = [
            rule for rule in rules if not rule.passed and rule.decision == "RESCHEDULE"
        ]
        chosen = (denials or deferrals or reschedules or [None])[0]
        object.__setattr__(instance, "decision", chosen.decision if chosen else "ALLOW")
        object.__setattr__(instance, "rules_evaluated", tuple(rules))
        object.__setattr__(instance, "reason", chosen.reason if chosen else None)
        object.__setattr__(instance, "retry_at", chosen.retry_at if chosen else None)
        return instance


class Gate:
    """Evaluate every safety rule; DENY outranks DEFER, which outranks RESCHEDULE."""

    def __init__(self, config: GuardrailConfig | None = None) -> None:
        self.config = config or get_policy_config().guardrails
        expected = [
            "SR1_PAID",
            "SR2_OPT_OUT",
            "SR3_DISPUTE",
            "SR4_BUDGET",
            "SR5_SUPPRESSED",
            "SR6_MERCHANT_FAULT",
            "SR7_PROTECTED",
        ]
        if [rule.id for rule in self.config.stopping_rules] != expected:
            raise ValueError("all seven stopping rules must be configured in safety order")

    @staticmethod
    def _result(
        rule_id: str,
        triggered: bool,
        decision: Decision,
        reason: str,
        *,
        retry_at: datetime | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> RuleResult:
        return RuleResult(
            rule_id=rule_id,
            passed=not triggered,
            decision=decision if triggered else "ALLOW",
            reason=reason,
            retry_at=retry_at,
            evidence=evidence,
        )

    def _stopping_rules(
        self,
        case: GateCase,
        customer: GateCustomer,
        diagnosis: GateDiagnosis,
        plan: GatePlan,
        action: PlannedAction,
        suppression_matches: bool,
    ) -> list[RuleResult]:
        rules = self.config.stopping_rules
        triggers = [
            case.verified_paid,
            customer.dnc,
            case.disputed,
            case.attempts >= plan.max_steps or case.retries >= 3,
            suppression_matches,
            diagnosis.failure_class == "MERCHANT_FAULT" and action.is_customer_facing,
            customer.protected,
        ]
        reasons = [
            "payment is already verified",
            "customer opted out",
            "amount or service is disputed",
            "plan or retry budget is exhausted",
            "a matching cohort suppression is open",
            "merchant fault cannot produce customer contact",
            "protected customer requires human handling",
        ]
        return [
            self._result(rule.id, triggered, rule.decision, reason)
            for rule, triggered, reason in zip(rules, triggers, reasons, strict=True)
        ]

    def _consent(self, action: PlannedAction) -> list[RuleResult]:
        required = action.channel in self.config.consent.required_for
        missing = action.is_customer_facing and required and not action.consent_granted
        return [
            self._result(
                "CONSENT_RECORDED",
                missing,
                self.config.consent.decision_if_absent,
                "recorded channel consent is required",
                evidence={"channel": action.channel, "basis": action.consent_basis},
            )
        ]

    def _frequency(
        self, action: PlannedAction, contacts: list[ContactRecord]
    ) -> list[RuleResult]:
        now = action.scheduled_for.astimezone(UTC)
        applicable = action.is_customer_facing
        recent = [
            contact
            for contact in contacts
            if now - timedelta(days=7) <= contact.sent_at.astimezone(UTC) <= now
        ]
        same = [contact for contact in recent if contact.channel == action.channel]
        any_last = max((item.sent_at.astimezone(UTC) for item in recent), default=None)
        same_last = max((item.sent_at.astimezone(UTC) for item in same), default=None)
        frequency = self.config.frequency
        return [
            self._result(
                "FREQ_CUSTOMER_7D",
                applicable and len(recent) >= frequency.max_contacts_per_customer_7d,
                "DENY",
                "rolling seven-day customer contact cap reached across all cases",
                evidence={"contacts": len(recent)},
            ),
            self._result(
                "FREQ_SAME_CHANNEL",
                applicable
                and same_last is not None
                and now - same_last < timedelta(hours=frequency.min_hours_between_same_channel),
                "DENY",
                "same-channel cooldown has not elapsed",
                evidence={"last_contact_at": same_last.isoformat() if same_last else None},
            ),
            self._result(
                "FREQ_ANY_CHANNEL",
                applicable
                and any_last is not None
                and now - any_last < timedelta(hours=frequency.min_hours_between_any_contact),
                "DENY",
                "cross-channel cooldown has not elapsed",
                evidence={"last_contact_at": any_last.isoformat() if any_last else None},
            ),
        ]

    def _next_window(self, scheduled_for: datetime, blackouts: set[date]) -> datetime:
        local = scheduled_for.astimezone(IST)
        window = self.config.schedule.contact_window_ist
        candidate_date = local.date()
        if local.time() >= window.end:
            candidate_date += timedelta(days=1)
        candidate = datetime.combine(candidate_date, window.start, tzinfo=IST)
        while candidate.date() in blackouts:
            candidate += timedelta(days=1)
        return candidate

    def _schedule(self, action: PlannedAction) -> list[RuleResult]:
        local = action.scheduled_for.astimezone(IST)
        window = self.config.schedule.contact_window_ist
        blackouts = {date.fromisoformat(item) for item in self.config.schedule.blackout_dates}
        outside = action.is_customer_facing and not (window.start <= local.time() < window.end)
        blackout = action.is_customer_facing and local.date() in blackouts
        retry_at = (
            self._next_window(action.scheduled_for, blackouts) if outside or blackout else None
        )
        return [
            self._result(
                "SCHEDULE_CONTACT_WINDOW",
                outside,
                self.config.schedule.decision_outside_window,
                "customer contact is outside 08:00-19:00 IST",
                retry_at=retry_at,
            ),
            self._result(
                "SCHEDULE_BLACKOUT",
                blackout,
                "RESCHEDULE",
                "customer contact falls on a blackout date",
                retry_at=retry_at,
            ),
        ]

    def _money(self, case: GateCase, action: PlannedAction) -> list[RuleResult]:
        money = self.config.money
        is_retry = action.action_type == "silent_retry"
        retry_limit = is_retry and case.retries >= money.max_retry_attempts_per_instrument
        backoff_index = min(case.retries, len(money.retry_backoff_hours) - 1)
        required_backoff = money.retry_backoff_hours[backoff_index]
        backoff_bad = (
            is_retry
            and action.last_retry_at is not None
            and action.scheduled_for.astimezone(UTC)
            < action.last_retry_at.astimezone(UTC) + timedelta(hours=required_backoff)
        )
        debit_over = action.makes_debit and (
            (
                action.mandate_max_amount_paise is not None
                and action.amount_paise > action.mandate_max_amount_paise
            )
            or (
                action.invoice_outstanding_paise is not None
                and action.amount_paise > action.invoice_outstanding_paise
            )
        )
        notice_bad = action.is_emandate and action.makes_debit and (
            action.pre_debit_notice_at is None
            or action.scheduled_for.astimezone(UTC)
            - action.pre_debit_notice_at.astimezone(UTC)
            < timedelta(hours=money.emandate.pre_debit_notice_hours)
        )
        ceiling = (
            money.emandate.afa_free_ceiling_special_paise
            if action.special_emandate_category
            else money.emandate.afa_free_ceiling_paise
        )
        afa_bad = (
            action.is_emandate
            and action.makes_debit
            and action.amount_paise > ceiling
            and not action.additional_factor_authenticated
        )
        needs_two_key = (
            action.amount_paise > money.two_key_above_paise
            or action.action_type in money.two_key_actions
            or action.channel == "voice"
        )
        two_key_bad = needs_two_key and not (
            action.human_confirmed or action.standing_merchant_approval
        )
        return [
            self._result("MONEY_RETRY_LIMIT", retry_limit, "DENY", "retry limit reached"),
            self._result(
                "MONEY_RETRY_BACKOFF",
                backoff_bad,
                "DENY",
                "mandatory retry backoff has not elapsed",
            ),
            self._result(
                "MONEY_AMOUNT_CEILING",
                debit_over,
                "DENY",
                "debit exceeds mandate maximum or invoice outstanding",
            ),
            self._result(
                "MONEY_PRE_DEBIT_NOTICE",
                notice_bad,
                "DENY",
                "e-mandate pre-debit notice is missing or too recent",
            ),
            self._result(
                "MONEY_EMANDATE_AFA",
                afa_bad,
                "DENY",
                "e-mandate debit above threshold requires additional authentication",
            ),
            self._result(
                "MONEY_TWO_KEY",
                two_key_bad,
                "DEFER_TO_HUMAN",
                "high-value or sensitive action requires a second key",
            ),
        ]

    @staticmethod
    def _message_integrity(action: PlannedAction) -> list[RuleResult]:
        needs_template = action.is_customer_facing and action.channel in {"whatsapp", "sms"}
        valid_template = isinstance(action.rendered_message, RenderedMessage) and (
            action.rendered_message.channel == action.channel
        )
        return [
            Gate._result(
                "MESSAGE_REGISTERED_TEMPLATE",
                needs_template and not valid_template,
                "DENY",
                "regulated messaging requires a matching RenderedMessage",
            )
        ]

    @staticmethod
    def _tone(action: PlannedAction, merchant_policy: dict[str, Any]) -> list[RuleResult]:
        allow_penalty = bool(merchant_policy.get("allow_penalty_language", False))
        return [
            Gate._result(
                "TONE_PENALTY",
                action.contains_penalty_language and not allow_penalty,
                "DENY",
                "penalty language is not enabled by merchant policy",
            ),
            Gate._result(
                "TONE_LEGAL",
                action.contains_legal_language,
                "DENY",
                "legal or consequence language is forbidden",
            ),
            Gate._result(
                "TONE_THIRD_PARTY",
                action.is_customer_facing and not action.recipient_is_customer,
                "DENY",
                "third-party contact is forbidden",
            ),
        ]

    def evaluate(
        self,
        case: GateCase,
        action: PlannedAction,
        *,
        customer: GateCustomer,
        diagnosis: GateDiagnosis,
        plan: GatePlan,
        contacts: list[ContactRecord] | None = None,
        suppression_matches: bool = False,
        merchant_policy: dict[str, Any] | None = None,
    ) -> GateVerdict:
        if action.scheduled_for.tzinfo is None:
            raise ValueError("scheduled_for must be timezone-aware")
        rules = self._stopping_rules(
            case, customer, diagnosis, plan, action, suppression_matches
        )
        rules.extend(self._consent(action))
        rules.extend(self._frequency(action, contacts or []))
        rules.extend(self._schedule(action))
        rules.extend(self._money(case, action))
        rules.extend(self._message_integrity(action))
        rules.extend(self._tone(action, merchant_policy or {}))
        return GateVerdict._from_gate(rules)


def record_gate_verdict(
    session: Session,
    case: RecoveryCase,
    action: Action,
    verdict: GateVerdict,
) -> None:
    """Persist the complete gate explanation on the action and case timeline."""
    serialized = []
    for rule in verdict.rules_evaluated:
        item = asdict(rule)
        item["retry_at"] = rule.retry_at.isoformat() if rule.retry_at else None
        serialized.append(item)
    action.verdict = verdict.decision
    action.verdict_rules = {"rules": serialized}
    append_event(
        session,
        case,
        kind="GATE",
        payload={
            "action_id": action.id,
            "decision": verdict.decision,
            "reason": verdict.reason,
            "retry_at": verdict.retry_at.isoformat() if verdict.retry_at else None,
            "rules_evaluated": serialized,
        },
        actor="guardrail_gate",
    )
