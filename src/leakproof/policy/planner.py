from __future__ import annotations

from calendar import monthrange
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from leakproof.audit.timeline import append_event
from leakproof.config import ActionConfig, LadderConfig, get_policy_config
from leakproof.models.db import Action, Consent, Customer, Diagnosis, Event, Merchant, RecoveryCase
from leakproof.models.domain import Arm, CaseOutcome, LeakType
from leakproof.policy.ev import FixedPriorPolicy, ScoredAction

IST = ZoneInfo("Asia/Kolkata")


class PlanningCase(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    leak_type: LeakType
    failure_class: str
    segment: str = "default"
    amount_at_risk_paise: int = Field(ge=0)
    arm: Arm = Arm.TREATMENT


class PlanningConstraints(BaseModel):
    model_config = ConfigDict(frozen=True)

    customer_contact_allowed: bool = True
    human_only: bool = False
    consented_channels: frozenset[str] = Field(default_factory=frozenset)
    max_customer_contacts: int | None = Field(default=None, ge=0)
    margin: float | None = Field(default=None, ge=0)
    annoyance_lambda: float | None = Field(default=None, ge=0)


class ActionEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    step_index: int
    action_type: str
    eligible: bool
    selected: bool
    score: ScoredAction | None = None
    reason: str


class PlanStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    step_index: int
    action_type: str
    scheduled_for: datetime
    channel: str | None
    customer_facing: bool
    requires_consent: bool
    two_key: bool
    ev_estimate_paise: int
    recovery_probability: float
    exploratory: bool
    rationale: str


class RecoveryPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    leak_type: LeakType
    failure_class: str
    segment: str
    amount_at_risk_paise: int
    arm: Arm
    status: Literal["PLANNED", "ABANDONED", "HOLDOUT"]
    ladder_id: str | None
    max_steps: int
    steps: tuple[PlanStep, ...]
    evaluations: tuple[ActionEvaluation, ...]
    reason: str
    fixed_prior_policy: bool = True


def _last_working_day(year: int, month: int) -> int:
    day = monthrange(year, month)[1]
    while datetime(year, month, day).weekday() >= 5:
        day -= 1
    return day


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def next_retry(failure_class: str, attempt: int, now: datetime) -> datetime | None:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if failure_class == "TIMING":
        local = now.astimezone(IST)
        year, month = local.year, local.month
        for _ in range(3):
            candidates = [
                datetime.combine(datetime(year, month, 1).date(), time(10), tzinfo=IST),
                datetime.combine(
                    datetime(year, month, _last_working_day(year, month)).date(),
                    time(10),
                    tzinfo=IST,
                ),
            ]
            future = [candidate for candidate in candidates if candidate > local]
            if future:
                return min(future).astimezone(now.tzinfo)
            year, month = _next_month(year, month)
        raise RuntimeError("could not determine the next payday")
    if failure_class == "TRANSIENT":
        backoffs = (6, 24, 72)
        return now + timedelta(hours=backoffs[attempt]) if attempt < len(backoffs) else None
    if failure_class == "FRICTION":
        return now + timedelta(minutes=30) if attempt == 0 else None
    if failure_class == "INSTRUMENT_DEAD":
        return None
    return None


class Planner:
    def __init__(
        self,
        *,
        actions: list[ActionConfig] | None = None,
        ladders: list[LadderConfig] | None = None,
        policy: FixedPriorPolicy | None = None,
    ) -> None:
        config = get_policy_config()
        configured_actions = actions or config.actions
        self.actions = {action.key: action for action in configured_actions}
        if len(self.actions) != len(configured_actions):
            raise ValueError("action keys must be unique")
        self.ladders = ladders or config.ladders
        self.policy = policy or FixedPriorPolicy()
        self._validate_ladders()

    def _validate_ladders(self) -> None:
        seen: set[tuple[str, str]] = set()
        for ladder in self.ladders:
            previous: tuple[int, int] | None = None
            if len(ladder.steps) > ladder.max_steps:
                raise ValueError(f"ladder {ladder.id} exceeds its max_steps bound")
            for step in ladder.steps:
                action = self.actions.get(step.action)
                if action is None:
                    raise ValueError(f"ladder {ladder.id} references unknown action {step.action}")
                severity = (action.cost_paise, action.intrusiveness)
                if previous is not None and severity < previous:
                    raise ValueError(f"ladder {ladder.id} is not cheapest-first")
                previous = severity
            for failure_class in ladder.failure_classes:
                key = (ladder.leak_type, failure_class)
                if key in seen:
                    raise ValueError(f"duplicate ladder mapping for {key}")
                seen.add(key)

    def _ladder_for(self, case: PlanningCase) -> LadderConfig | None:
        return next(
            (
                ladder
                for ladder in self.ladders
                if ladder.leak_type == case.leak_type.value
                and case.failure_class in ladder.failure_classes
            ),
            None,
        )

    @staticmethod
    def _static_eligibility(
        action: ActionConfig,
        case: PlanningCase,
        constraints: PlanningConstraints,
        customer_contacts: int,
    ) -> str | None:
        if action.applicable_to and case.failure_class not in action.applicable_to:
            return f"{action.key} is not applicable to {case.failure_class}"
        if constraints.human_only and action.key != "human_handoff":
            return "case is restricted to human handling"
        if action.customer_facing and not constraints.customer_contact_allowed:
            return "customer contact is prohibited for this case"
        if (
            action.customer_facing
            and constraints.max_customer_contacts is not None
            and customer_contacts >= constraints.max_customer_contacts
        ):
            return "diagnosis contact budget is exhausted"
        if action.requires_consent and action.channel not in constraints.consented_channels:
            return f"recorded {action.channel} consent is absent"
        return None

    def plan(
        self,
        case: PlanningCase,
        *,
        now: datetime,
        constraints: PlanningConstraints | None = None,
    ) -> RecoveryPlan:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if case.arm == Arm.HOLDOUT:
            return RecoveryPlan(
                case_id=case.case_id,
                leak_type=case.leak_type,
                failure_class=case.failure_class,
                segment=case.segment,
                amount_at_risk_paise=case.amount_at_risk_paise,
                arm=case.arm,
                status="HOLDOUT",
                ladder_id=None,
                max_steps=0,
                steps=(),
                evaluations=(),
                reason=(
                    "Randomized holdout: diagnosis is retained for measurement, "
                    "but no intervention or contact is permitted."
                ),
            )
        applied = constraints or PlanningConstraints()
        ladder = self._ladder_for(case)
        if ladder is None:
            return RecoveryPlan(
                case_id=case.case_id,
                leak_type=case.leak_type,
                failure_class=case.failure_class,
                segment=case.segment,
                amount_at_risk_paise=case.amount_at_risk_paise,
                arm=case.arm,
                status="ABANDONED",
                ladder_id=None,
                max_steps=0,
                steps=(),
                evaluations=(),
                reason=(
                    f"No bounded ladder is registered for {case.leak_type.value}/"
                    f"{case.failure_class}."
                ),
            )

        steps: list[PlanStep] = []
        evaluations: list[ActionEvaluation] = []
        cursor = now
        retry_attempt = 0
        customer_contacts = 0
        for configured_index, configured_step in enumerate(ladder.steps):
            action = self.actions[configured_step.action]
            ineligible = self._static_eligibility(
                action, case, applied, customer_contacts
            )
            if ineligible is not None:
                evaluations.append(
                    ActionEvaluation(
                        step_index=configured_index,
                        action_type=action.key,
                        eligible=False,
                        selected=False,
                        reason=ineligible,
                    )
                )
                continue

            score = self.policy.score(
                action,
                failure_class=case.failure_class,
                segment=case.segment,
                amount_at_risk_paise=case.amount_at_risk_paise,
                margin=applied.margin,
                annoyance_lambda=applied.annoyance_lambda,
            )
            if score.ev_paise <= 0:
                evaluations.append(
                    ActionEvaluation(
                        step_index=configured_index,
                        action_type=action.key,
                        eligible=True,
                        selected=False,
                        score=score,
                        reason=f"EV {score.ev_paise} paise is not positive",
                    )
                )
                continue

            if action.key == "silent_retry":
                scheduled_for = next_retry(case.failure_class, retry_attempt, cursor)
                retry_attempt += 1
                if scheduled_for is None:
                    evaluations.append(
                        ActionEvaluation(
                            step_index=configured_index,
                            action_type=action.key,
                            eligible=False,
                            selected=False,
                            score=score,
                            reason=f"{case.failure_class} has no safe retry time",
                        )
                    )
                    continue
            else:
                scheduled_for = cursor + timedelta(hours=configured_step.delay_hours)

            step_index = len(steps)
            rationale = (
                f"Positive EV {score.ev_paise} paise using {score.prior_source}; "
                f"P(recover)={score.probability:.3f}."
            )
            steps.append(
                PlanStep(
                    step_index=step_index,
                    action_type=action.key,
                    scheduled_for=scheduled_for,
                    channel=action.channel,
                    customer_facing=action.customer_facing,
                    requires_consent=action.requires_consent,
                    two_key=action.two_key,
                    ev_estimate_paise=score.ev_paise,
                    recovery_probability=score.probability,
                    exploratory=score.exploratory,
                    rationale=rationale,
                )
            )
            evaluations.append(
                ActionEvaluation(
                    step_index=configured_index,
                    action_type=action.key,
                    eligible=True,
                    selected=True,
                    score=score,
                    reason=rationale,
                )
            )
            cursor = scheduled_for
            customer_contacts += int(action.customer_facing)

        if not steps:
            explanations = "; ".join(
                f"{item.action_type}: {item.reason}" for item in evaluations
            )
            return RecoveryPlan(
                case_id=case.case_id,
                leak_type=case.leak_type,
                failure_class=case.failure_class,
                segment=case.segment,
                amount_at_risk_paise=case.amount_at_risk_paise,
                arm=case.arm,
                status="ABANDONED",
                ladder_id=ladder.id,
                max_steps=ladder.max_steps,
                steps=(),
                evaluations=tuple(evaluations),
                reason=f"No guardrail-eligible action has positive EV. {explanations}",
            )

        return RecoveryPlan(
            case_id=case.case_id,
            leak_type=case.leak_type,
            failure_class=case.failure_class,
            segment=case.segment,
            amount_at_risk_paise=case.amount_at_risk_paise,
            arm=case.arm,
            status="PLANNED",
            ladder_id=ladder.id,
            max_steps=ladder.max_steps,
            steps=tuple(steps),
            evaluations=tuple(evaluations),
            reason=(
                f"Selected {len(steps)} bounded, positive-EV step(s) cheapest-first. "
                "Estimates use fixed prior means, not sampled outcomes."
            ),
        )


def plan_case(
    session: Session,
    case_id: str,
    *,
    now: datetime | None = None,
    planner: Planner | None = None,
) -> RecoveryPlan:
    """Render, persist, and audit one idempotent recovery schedule."""
    case = session.get(RecoveryCase, case_id)
    if case is None:
        raise LookupError(case_id)
    previous = session.scalar(
        select(Event)
        .where(Event.case_id == case_id, Event.kind.in_(["PLANNED", "CLOSED"]))
        .order_by(Event.seq.desc())
    )
    if previous is not None and "plan" in previous.payload:
        result = RecoveryPlan.model_validate(previous.payload["plan"])
        _persist_schedule(session, result)
        return result

    diagnosis = session.get(Diagnosis, case_id)
    if diagnosis is None:
        raise ValueError(f"case {case_id} must be diagnosed before planning")
    customer = session.get(Customer, case.customer_id)
    merchant = session.get(Merchant, case.merchant_id)
    if customer is None or merchant is None:
        raise ValueError(f"case {case_id} is missing its customer or merchant")
    consented = frozenset(
        session.scalars(
            select(Consent.channel).where(
                Consent.customer_id == customer.id, Consent.granted.is_(True)
            )
        )
    )
    policy = merchant.policy or {}
    planning_case = PlanningCase(
        case_id=case.id,
        leak_type=case.leak_type,
        failure_class=diagnosis.failure_class,
        segment=customer.segment or "default",
        amount_at_risk_paise=case.amount_at_risk,
        arm=case.arm,
    )
    constraints = PlanningConstraints(
        customer_contact_allowed=not customer.dnc
        and diagnosis.failure_class != "MERCHANT_FAULT",
        human_only=customer.protected or diagnosis.failure_class == "DISPUTED",
        consented_channels=consented,
        max_customer_contacts=1 if diagnosis.failure_class == "INTENT_ABSENT" else None,
        margin=policy.get("margin", 1.0),
        annoyance_lambda=policy.get("annoyance_lambda", 0.02),
    )
    planned_at = now or datetime.now(UTC)
    result = (planner or Planner()).plan(
        planning_case, now=planned_at, constraints=constraints
    )
    payload = {"plan": result.model_dump(mode="json")}
    if result.status == "ABANDONED":
        case.outcome = CaseOutcome.ABANDONED.value
        case.closed_at = planned_at
        payload.update({"outcome": "ABANDONED", "reason": result.reason})
        append_event(session, case, kind="CLOSED", payload=payload, actor="policy_planner")
    else:
        _persist_schedule(session, result)
        append_event(session, case, kind="PLANNED", payload=payload, actor="policy_planner")
    session.flush()
    return result


def _action_id(case_id: str, step_index: int) -> str:
    import hashlib

    digest = hashlib.sha256(f"{case_id}:{step_index}".encode()).hexdigest()[:24]
    return f"act_{digest}"


def _persist_schedule(session: Session, plan: RecoveryPlan) -> None:
    """Materialize plan steps once; the unique case/step key is the final backstop."""
    if plan.status != "PLANNED":
        return
    costs = {item.key: item.cost_paise for item in get_policy_config().actions}
    existing = set(
        session.scalars(select(Action.step_index).where(Action.case_id == plan.case_id))
    )
    for step in plan.steps:
        if step.step_index in existing:
            continue
        session.add(
            Action(
                id=_action_id(plan.case_id, step.step_index),
                case_id=plan.case_id,
                step_index=step.step_index,
                action_type=step.action_type,
                scheduled_for=step.scheduled_for,
                status="pending",
                cost_paise=costs[step.action_type],
                ev_estimate=step.ev_estimate_paise,
            )
        )
    session.flush()
