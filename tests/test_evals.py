from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from leakproof.diagnosis.tier2 import CohortScanInput
from leakproof.evals import run_all_evals, run_cohort_eval, run_injection_eval
from leakproof.models.db import EvalRun

COHORT = Path("evals/cohort/cases.jsonl")
INJECTION = Path("evals/injection/corpus.jsonl")


def test_cohort_contract_rejects_undeclared_identifier_fields():
    row = json.loads(COHORT.read_text(encoding="utf-8").splitlines()[0])
    row["input"]["customer_id"] = "cust_should_not_cross_boundary"

    with pytest.raises(ValidationError):
        CohortScanInput.model_validate(row["input"])


def test_cohort_corpus_meets_composition_and_quality_gates():
    result = run_cohort_eval(COHORT, baseline_f1=1.0)

    assert result["passed"] is True
    assert result["case_count"] == 120
    assert result["composition"] == {"anomaly": 40, "clean": 30, "near_miss": 50}
    assert result["negative_count"] == 80
    assert result["overall"] == {
        "true_positives": 40,
        "false_positives": 0,
        "false_negatives": 0,
        "precision": 1.0,
        "recall": 1.0,
        "f1": 1.0,
    }
    assert result["false_suppression_rate"] == 0.0
    assert all(metrics["f1"] == 1.0 for metrics in result["per_pattern"].values())


def test_cohort_gate_rejects_more_than_two_point_f1_regression():
    result = run_cohort_eval(COHORT, baseline_f1=1.03)

    assert result["passed"] is False
    assert result["gates"]["f1_regression_within_2pp"] is False


def test_false_suppression_gate_fails_on_a_qualified_negative(tmp_path):
    rows = [json.loads(line) for line in COHORT.read_text(encoding="utf-8").splitlines()]
    near_misses = [row for row in rows if row["kind"] == "near_miss"][:5]
    for near_miss in near_misses:
        near_miss["input"]["slices"][0].update(
            {"attempts": 50, "failures": 30, "baseline_rate": 0.05}
        )
        near_miss["input"]["slices"][0]["top_reasons"] = {
            "gateway_technical_error": 30
        }
    corpus = tmp_path / "cases.jsonl"
    corpus.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    result = run_cohort_eval(corpus, baseline_f1=1.0)

    assert result["false_suppressions"] == 5
    assert result["false_suppression_rate"] > 0.05
    assert result["gates"]["false_suppression_rate_lte_0_05"] is False
    assert result["passed"] is False


def test_injection_suite_has_zero_bypasses_and_preserves_benign_inputs():
    result = run_injection_eval(INJECTION)

    assert result["passed"] is True
    assert result["case_count"] == 64
    assert result["attack_cases"] == 56
    assert result["benign_lookalikes"] == 8
    assert result["bypasses"] == 0
    assert result["attack_success_summary"] == "0/56"


def test_full_eval_persists_both_suites_and_latest_api_reports_them(
    session_factory, client, tmp_path
):
    with session_factory() as session:
        report = run_all_evals(report_path=tmp_path / "report.json", session=session)
        assert report.overall_passed is True
        assert session.scalar(select(func.count()).select_from(EvalRun)) == 2

    response = client.get("/evals/latest")

    assert response.status_code == 200
    payload = response.json()
    assert payload["overall_passed"] is True
    assert {item["suite"] for item in payload["runs"]} == {"cohort", "injection"}


def test_latest_evals_returns_not_found_before_first_run(client):
    response = client.get("/evals/latest")

    assert response.status_code == 404
