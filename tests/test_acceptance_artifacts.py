from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "validate_acceptance_artifacts",
    Path(__file__).parents[1] / "scripts" / "validate_acceptance_artifacts.py",
)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def _artifact() -> dict:
    checks = [
        {
            "check": name,
            "passed": True,
            "severity": "blocking",
            "detail": "Sanitized release evidence passed.",
        }
        for name in sorted(validator.REQUIRED_CHECKS)
    ]
    return {
        "schema_version": "2026-09-04",
        "data_provenance": "LIVE_PROVIDER_VERIFIED",
        "exported_at": "2026-09-04T12:00:00Z",
        "passed": True,
        "session": {
            "state": "RECOVERED",
            "amount_paise": 50000,
            "currency": "INR",
            "email_mode": "preview_only",
        },
        "case": {
            "leak_type": "CHECKOUT_ABANDON",
            "state": "CLOSED",
            "deterministic_diagnosis_ready": True,
            "insight_status": "succeeded",
        },
        "operational_metrics": {
            "cases_detected": 1,
            "recovered_cases": 1,
            "recovered_amount_paise": 50000,
            "recovery_rate": 1.0,
            "median_recovery_time_seconds": 42.0,
            "provider_failures": 0,
            "luna_cost_paise": 1,
        },
        "provider_statuses": [],
        "timeline": [],
        "checks": checks,
    }


def test_acceptance_artifact_validator_accepts_complete_sanitized_live_evidence(tmp_path):
    path = tmp_path / "checkout.json"
    path.write_text(json.dumps(_artifact()))

    artifact = validator.validate_file(path, require_live=True)

    assert artifact.passed is True
    assert artifact.case.leak_type == "CHECKOUT_ABANDON"


def test_acceptance_validator_accepts_subscription_cycle_evidence(tmp_path):
    payload = _artifact()
    payload["session"]["scenario_type"] = "SUBSCRIPTION_HALT"
    payload["case"]["leak_type"] = "SUBSCRIPTION_HALT"
    required = {
        "case_detected",
        "pending_to_halted_same_case",
        "razorpay_owns_retries",
        "no_app_owned_debit",
        "method_update_rechecked",
        "cycle_payment_ledger_unique",
        "audit_projection_replay_matches",
        "no_blocking_provider_failure",
        "intentional_states_have_no_cta",
        "exact_invoice_settled",
        "same_case_closed",
        "recovered_revenue_is_captured",
    }
    payload["checks"] = [
        {
            "check": name,
            "passed": True,
            "severity": "blocking",
            "detail": "Sanitized subscription evidence passed.",
        }
        for name in sorted(required)
    ]
    payload["subscription"] = {
        "provider_status": "active",
        "payment_method": "card",
        "cycle_resolved": True,
        "cycle_status": "paid",
        "detected_balance_paise": 50000,
        "outstanding_balance_paise": 0,
        "recovered_paise": 50000,
        "retry_owner": "razorpay",
        "retry_count": 3,
        "method_update_available": False,
        "disposition": "paid",
        "last_checked_at": "2026-09-04T12:00:00Z",
    }
    path = tmp_path / "subscription.json"
    path.write_text(json.dumps(payload))

    artifact = validator.validate_file(path, require_live=True)

    assert artifact.passed and artifact.subscription.retry_owner == "razorpay"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("session_token", "secret-value", "forbidden identifier field"),
        ("note", "reviewer@example.com", "email address"),
        ("note", "https://demo.example/recover/signed-value", "signed recovery URL"),
        ("note", "order_private123", "Razorpay entity identifier"),
    ],
)
def test_acceptance_artifact_validator_rejects_sensitive_identifiers(
    tmp_path, field, value, message
):
    payload = _artifact()
    payload[field] = value
    path = tmp_path / "unsafe.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match=message):
        validator.validate_file(path, require_live=True)


def test_acceptance_artifact_validator_rejects_missing_day4_check(tmp_path):
    payload = _artifact()
    payload["checks"] = payload["checks"][1:]
    path = tmp_path / "incomplete.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="missing required checks"):
        validator.validate_file(path, require_live=True)


def test_telemetry_reconciled_capture_requires_abandonment_evidence(tmp_path):
    payload = _artifact()
    payload["data_provenance"] = "LIVE_TELEMETRY_PROVIDER_RECONCILED"
    path = tmp_path / "telemetry.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="missing required checks"):
        validator.validate_file(path, require_live=True)
    payload["checks"].extend(
        {"check": check, "passed": True, "severity": "blocking", "detail": "contract assertion"}
        for check in validator.ABANDONMENT_CHECKS
    )
    path.write_text(json.dumps(payload))
    assert validator.validate_file(path, require_live=True).passed
    payload["data_provenance"] = "SIMULATED_END_TO_END"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="not live provider evidence"):
        validator.validate_file(path, require_live=True)
