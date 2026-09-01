from __future__ import annotations

from collections.abc import Iterable
from statistics import median

from pydantic import BaseModel, ConfigDict, Field

from leakproof.measurement.scoreboard import EstimateInterval, Scoreboard


class SensitivityScenario(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    treatment_effect_multiplier: float = Field(ge=0)
    seed_count: int = Field(ge=2)
    seeds: list[int]
    estimator: str
    confidence_level: float
    assumption_hashes: list[str]
    lift_percentage_points: EstimateInterval
    incremental_revenue_paise: EstimateInterval
    contribution_margin_paise: EstimateInterval
    net_economic_value_paise: EstimateInterval


class SensitivityReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    synthetic: bool = True
    isolated_evaluation_database: bool = True
    interval_method: str = "empirical_percentile_across_seeds"
    scenarios: list[SensitivityScenario]


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def empirical_interval(values: Iterable[float], confidence_level: float) -> EstimateInterval:
    samples = [float(value) for value in values]
    if len(samples) < 2:
        raise ValueError("empirical uncertainty requires at least two seeds")
    tail = (1 - confidence_level) / 2
    return EstimateInterval(
        median=round(median(samples), 3),
        minimum=round(min(samples), 3),
        maximum=round(max(samples), 3),
        interval_low=round(_percentile(samples, tail), 3),
        interval_high=round(_percentile(samples, 1 - tail), 3),
    )


def summarize_scenario(
    scoreboards: list[Scoreboard],
    *,
    seeds: list[int],
    treatment_effect_multiplier: float,
) -> SensitivityScenario:
    if len(scoreboards) != len(seeds):
        raise ValueError("each sensitivity seed must have exactly one scoreboard")
    if len(scoreboards) < 2:
        raise ValueError("sensitivity scenarios require at least two seeds")
    confidence_level = scoreboards[0].uncertainty.confidence_level
    return SensitivityScenario(
        treatment_effect_multiplier=treatment_effect_multiplier,
        seed_count=len(seeds),
        seeds=seeds,
        estimator=scoreboards[0].estimator,
        confidence_level=confidence_level,
        assumption_hashes=sorted({item.assumption_hash for item in scoreboards}),
        lift_percentage_points=empirical_interval(
            (item.lift_percentage_points for item in scoreboards), confidence_level
        ),
        incremental_revenue_paise=empirical_interval(
            (item.incremental_revenue_paise for item in scoreboards), confidence_level
        ),
        contribution_margin_paise=empirical_interval(
            (item.contribution_margin_paise for item in scoreboards), confidence_level
        ),
        net_economic_value_paise=empirical_interval(
            (item.net_economic_value_paise for item in scoreboards), confidence_level
        ),
    )
