from __future__ import annotations

from types import SimpleNamespace

import pytest

from leakproof.measurement.sensitivity import empirical_interval, summarize_scenario


def _scoreboard(value: int, assumption_hash: str) -> SimpleNamespace:
    return SimpleNamespace(
        estimator="stratified_holdout_amount_rate",
        assumption_hash=assumption_hash,
        uncertainty=SimpleNamespace(confidence_level=0.8),
        lift_percentage_points=float(value),
        incremental_revenue_paise=value * 100,
        contribution_margin_paise=value * 68,
        net_economic_value_paise=value * 60,
    )


def test_empirical_interval_reports_median_range_and_declared_percentiles():
    interval = empirical_interval([1, 2, 10, 20, 40], 0.8)

    assert interval.median == 10
    assert interval.minimum == 1
    assert interval.maximum == 40
    assert interval.interval_low == 1.4
    assert interval.interval_high == 32


def test_sensitivity_summary_carries_seed_count_estimator_and_assumption_hashes():
    summary = summarize_scenario(
        [_scoreboard(1, "hash-a"), _scoreboard(3, "hash-a"), _scoreboard(5, "hash-b")],
        seeds=[41, 42, 43],
        treatment_effect_multiplier=0.25,
    )

    assert summary.seed_count == 3
    assert summary.seeds == [41, 42, 43]
    assert summary.assumption_hashes == ["hash-a", "hash-b"]
    assert summary.lift_percentage_points.median == 3
    assert summary.incremental_revenue_paise.minimum == 100
    assert summary.net_economic_value_paise.maximum == 300


def test_sensitivity_requires_paired_seed_and_scoreboard_counts():
    with pytest.raises(ValueError, match="exactly one scoreboard"):
        summarize_scenario(
            [_scoreboard(1, "hash")],
            seeds=[41, 42],
            treatment_effect_multiplier=1.0,
        )
    with pytest.raises(ValueError, match="at least two seeds"):
        empirical_interval([1], 0.8)
