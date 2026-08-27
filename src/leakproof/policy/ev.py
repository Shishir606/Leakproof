from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field

from leakproof.config import (
    ActionConfig,
    PolicyDefaultsConfig,
    PriorCell,
    PriorsConfig,
    get_policy_config,
)


class PriorEstimate(BaseModel):
    model_config = ConfigDict(frozen=True)

    probability: float = Field(ge=0, le=1)
    alpha: float = Field(gt=0)
    beta: float = Field(gt=0)
    observations: float = Field(gt=0)
    source: str
    exploratory: bool


class ScoredAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    action_type: str
    probability: float
    amount_at_risk_paise: int
    expected_recovery_paise: int
    direct_cost_paise: int
    annoyance_cost_paise: int
    ev_paise: int
    margin: float
    annoyance_lambda: float
    intrusiveness: int
    prior_source: str
    prior_observations: float
    exploratory: bool


def _paise(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class FixedPriorPolicy:
    """Deterministic posterior means only; no sampling or online updates in this slice."""

    def __init__(
        self,
        priors: PriorsConfig | None = None,
        defaults: PolicyDefaultsConfig | None = None,
    ) -> None:
        config = get_policy_config()
        self.priors = priors or config.priors
        self.defaults = defaults or config.policy_defaults

    def estimate(self, failure_class: str, segment: str, action_type: str) -> PriorEstimate:
        segment_cell = (
            self.priors.segment_cells.get(failure_class, {})
            .get(segment, {})
            .get(action_type)
        )
        class_cell = self.priors.cells.get(failure_class, {}).get(action_type)
        cell: PriorCell = segment_cell or class_cell or self.priors.defaults
        source = (
            f"segment:{failure_class}:{segment}:{action_type}"
            if segment_cell is not None
            else f"class:{failure_class}:{action_type}"
            if class_cell is not None
            else "defaults"
        )
        observations = cell.alpha + cell.beta
        return PriorEstimate(
            probability=cell.alpha / observations,
            alpha=cell.alpha,
            beta=cell.beta,
            observations=observations,
            source=source,
            exploratory=observations < self.defaults.exploratory_below_observations,
        )

    def score(
        self,
        action: ActionConfig,
        *,
        failure_class: str,
        segment: str,
        amount_at_risk_paise: int,
        margin: float | None = None,
        annoyance_lambda: float | None = None,
    ) -> ScoredAction:
        if amount_at_risk_paise < 0:
            raise ValueError("amount_at_risk_paise cannot be negative")
        applied_margin = self.defaults.margin if margin is None else margin
        applied_lambda = (
            self.defaults.annoyance_lambda if annoyance_lambda is None else annoyance_lambda
        )
        if applied_margin < 0 or applied_lambda < 0:
            raise ValueError("margin and annoyance_lambda cannot be negative")

        prior = self.estimate(failure_class, segment, action.key)
        amount = Decimal(amount_at_risk_paise)
        recovery = (
            Decimal(str(prior.probability)) * amount * Decimal(str(applied_margin))
        )
        annoyance = (
            Decimal(str(applied_lambda)) * Decimal(action.intrusiveness) * amount
        )
        ev = recovery - Decimal(action.cost_paise) - annoyance
        return ScoredAction(
            action_type=action.key,
            probability=prior.probability,
            amount_at_risk_paise=amount_at_risk_paise,
            expected_recovery_paise=_paise(recovery),
            direct_cost_paise=action.cost_paise,
            annoyance_cost_paise=_paise(annoyance),
            ev_paise=_paise(ev),
            margin=applied_margin,
            annoyance_lambda=applied_lambda,
            intrusiveness=action.intrusiveness,
            prior_source=prior.source,
            prior_observations=prior.observations,
            exploratory=prior.exploratory,
        )
