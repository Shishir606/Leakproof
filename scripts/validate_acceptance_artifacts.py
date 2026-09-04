"""Validate sanitized Day 4 provider-rehearsal acceptance artifacts."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from leakproof.demo.contracts import DemoAcceptanceExport
from leakproof.provenance import DataProvenance

REQUIRED_CHECKS = {
    "case_detected",
    "deterministic_diagnosis_ready",
    "insight_or_fallback_ready",
    "recovery_action_registered",
    "email_action_exercised",
    "original_order_recovered",
    "original_order_reused",
    "provider_verified_payment",
    "same_case_closed",
    "pending_contacts_cancelled",
    "session_recovered_amount_correct",
    "audit_projection_replay_matches",
    "no_blocking_provider_failure",
}
ABANDONMENT_CHECKS = {
    "browser_dismissal_recorded",
    "unpaid_order_rechecked",
    "original_order_reopened",
}
INVOICE_CHECKS = {
    "case_detected",
    "original_invoice_reused",
    "invoice_due_policy_recorded",
    "invoice_payment_ledger_unique",
    "captured_payment_globally_unique",
    "audit_projection_replay_matches",
    "no_blocking_provider_failure",
    "pending_contacts_cancelled",
}
SUBSCRIPTION_CHECKS = {
    "case_detected",
    "pending_to_halted_same_case",
    "razorpay_owns_retries",
    "no_app_owned_debit",
    "method_update_rechecked",
    "cycle_payment_ledger_unique",
    "captured_payment_globally_unique",
    "audit_projection_replay_matches",
    "no_blocking_provider_failure",
    "intentional_states_have_no_cta",
}

FORBIDDEN_KEY_FRAGMENTS = {
    "action_id",
    "attempt_id",
    "browser_attempt",
    "order_id",
    "invoice_id",
    "payment_id",
    "customer_id",
    "short_url",
    "redirect_url",
    "provider_ref",
    "recipient",
    "recovery_path",
    "recovery_url",
    "request_id",
    "session_id",
    "token",
}
FORBIDDEN_VALUE_PATTERNS = {
    "email address": re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b"),
    "bearer credential": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    "signed recovery URL": re.compile(r"(?i)(?:https?://\S+)?/recover/[A-Za-z0-9_.-]+"),
    "Razorpay entity identifier": re.compile(
        r"\b(?:order|pay|evt|inv|sub|cust|token)_[A-Za-z0-9_-]+\b"
    ),
    "session credential": re.compile(r"\bdemo_[0-9a-f]{16,}\b"),
}


def _walk(value: Any, path: str = "$"):
    if isinstance(value, dict):
        for key, item in value.items():
            yield f"{path}.{key}", key, item
            yield from _walk(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")


def validate_file(path: Path, *, require_live: bool) -> DemoAcceptanceExport:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: invalid acceptance artifact: {exc}") from exc

    errors: list[str] = []
    for item_path, key, item in _walk(raw):
        normalized_key = key.casefold()
        if any(fragment in normalized_key for fragment in FORBIDDEN_KEY_FRAGMENTS):
            errors.append(f"forbidden identifier field at {item_path}")
        if isinstance(item, str):
            for label, pattern in FORBIDDEN_VALUE_PATTERNS.items():
                if pattern.search(item):
                    errors.append(f"{label} found at {item_path}")
    if errors:
        raise ValueError(f"{path}: " + "; ".join(errors))

    try:
        artifact = DemoAcceptanceExport.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"{path}: invalid acceptance artifact: {exc}") from exc

    checks = {item.check: item for item in artifact.checks}
    declared_scenario = artifact.session.scenario_type
    scenario = declared_scenario or (artifact.case.leak_type if artifact.case else None)
    required = REQUIRED_CHECKS.copy()
    if declared_scenario == "CHECKOUT_ABANDON":
        required |= ABANDONMENT_CHECKS
    if scenario == "INVOICE_OVERDUE":
        required = INVOICE_CHECKS.copy()
        if artifact.invoice and artifact.invoice.disposition == "merchant_review":
            required |= {
                "nonpayable_invoice_has_no_payment_cta",
                "nonpayable_invoice_not_recovered",
            }
        else:
            required |= {
                "invoice_partial_payment_kept_open",
                "original_invoice_opened",
                "same_case_closed",
                "session_recovered_amount_correct",
                "provider_verified_payment",
            }
        if artifact.case is None or artifact.case.leak_type != "INVOICE_OVERDUE":
            errors.append("invoice evidence requires an invoice-overdue case")
    if scenario == "SUBSCRIPTION_HALT":
        required = SUBSCRIPTION_CHECKS.copy()
        if artifact.session.state == "RECOVERED":
            required |= {
                "exact_invoice_settled",
                "same_case_closed",
                "recovered_revenue_is_captured",
            }
        else:
            required.add("activation_not_counted_as_revenue")
        if artifact.case is None or artifact.case.leak_type != "SUBSCRIPTION_HALT":
            errors.append("subscription evidence requires a subscription-halt case")
    if artifact.data_provenance == DataProvenance.LIVE_TELEMETRY_PROVIDER_RECONCILED:
        required |= ABANDONMENT_CHECKS
        if artifact.case is None or artifact.case.leak_type != "CHECKOUT_ABANDON":
            errors.append("telemetry-reconciled evidence requires a checkout-abandonment case")
    if declared_scenario and artifact.case and declared_scenario != artifact.case.leak_type:
        errors.append("session scenario and case leak type do not match")
    if artifact.data_provenance == DataProvenance.ARCHITECTURE_READY:
        errors.append("architecture readiness is not acceptance evidence")
    missing = sorted(required - checks.keys())
    if missing:
        errors.append("missing required checks: " + ", ".join(missing))
    failed = sorted(
        item.check for item in artifact.checks if item.severity == "blocking" and not item.passed
    )
    if failed:
        errors.append("blocking checks failed: " + ", ".join(failed))
    if not artifact.passed:
        errors.append("artifact passed flag is false")
    if require_live and artifact.data_provenance not in {
        DataProvenance.LIVE_PROVIDER_VERIFIED,
        DataProvenance.LIVE_TELEMETRY_PROVIDER_RECONCILED,
    }:
        errors.append("artifact is not live provider evidence")

    if errors:
        raise ValueError(f"{path}: " + "; ".join(errors))
    return artifact


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="*", type=Path)
    parser.add_argument(
        "--directory",
        type=Path,
        help="Validate every JSON artifact in this directory.",
    )
    parser.add_argument("--require-live", action="store_true")
    parser.add_argument("--require-both-hero-paths", action="store_true")
    parser.add_argument("--require-all-scenarios", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = list(args.artifacts)
    if args.directory is not None:
        paths.extend(sorted(args.directory.glob("*.json")))
    if not paths:
        print("no acceptance artifacts were supplied", file=sys.stderr)
        return 2
    validated: list[DemoAcceptanceExport] = []
    try:
        for path in paths:
            validated.append(validate_file(path, require_live=args.require_live))
        if args.require_both_hero_paths:
            found = {item.case.leak_type for item in validated if item.case is not None}
            required = {"PAYMENT_FAILURE", "CHECKOUT_ABANDON"}
            if len(validated) != 2 or found != required:
                raise ValueError(
                    "expected one successful artifact for each hero path; found "
                    + ", ".join(sorted(found))
                )
        if args.require_all_scenarios:
            found = {
                item.session.scenario_type or (item.case.leak_type if item.case else None)
                for item in validated
            }
            required = {
                "PAYMENT_FAILURE",
                "CHECKOUT_ABANDON",
                "INVOICE_OVERDUE",
                "SUBSCRIPTION_HALT",
            }
            missing = sorted(required - found)
            if missing:
                raise ValueError("missing acceptance scenarios: " + ", ".join(missing))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    provider_evidence = sum(
        item.data_provenance
        in {
            DataProvenance.LIVE_PROVIDER_VERIFIED,
            DataProvenance.LIVE_TELEMETRY_PROVIDER_RECONCILED,
        }
        for item in validated
    )
    contract_evidence = sum(
        item.data_provenance == DataProvenance.CONTRACT_VERIFIED for item in validated
    )
    simulated_evidence = len(validated) - provider_evidence - contract_evidence
    print(
        f"validated {len(validated)} sanitized acceptance artifact(s): "
        f"provider={provider_evidence}, contract={contract_evidence}, "
        f"simulated={simulated_evidence}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
