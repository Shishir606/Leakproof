"""Capture sanitized Day 4 evidence for the synthetic HDFC incident and safe fallback."""

from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from leakproof.db import Base
from leakproof.diagnosis.tier2 import (
    DeterministicCohortProvider,
    aggregate_cohort_window,
    qualified_slices,
    run_cohort_scan,
)
from leakproof.models.db import Event, LLMCall, RecoveryCase, Suppression
from leakproof.models.domain import CaseState
from leakproof.providers import ProviderError
from leakproof.simulator.generate import generate_dataset, load_parameters
from leakproof.simulator.seed import persist_dataset


class DisabledCohortProvider:
    def analyze_cohort(self, _request):
        raise ProviderError(
            provider="openai",
            operation="cohort_analysis",
            error_class="disabled",
            retryable=False,
            message="model disabled for release fallback rehearsal",
        )


def _engine():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def _window(dataset):
    signals = [item for item in dataset.signals if item.scenario == "issuer_outage"]
    return (
        min(item.occurred_at for item in signals),
        max(item.occurred_at for item in signals) + timedelta(seconds=1),
    )


def _outage_ids(dataset) -> set[str]:
    return {item.entity_id for item in dataset.signals if item.scenario == "issuer_outage"}


def capture() -> dict:
    dataset = generate_dataset(load_parameters())
    window_from, window_to = _window(dataset)
    outage_ids = _outage_ids(dataset)
    accepted_engine = _engine()
    try:
        with Session(accepted_engine, expire_on_commit=False) as session:
            persist_dataset(session, dataset)
            aggregate = aggregate_cohort_window(
                session,
                merchant_id=dataset.merchant_id,
                window_from=window_from,
                window_to=window_to,
                batch_run_id=dataset.run_id,
            )
            candidates = qualified_slices(aggregate)
            sanitized_payload = aggregate.model_dump(mode="json", by_alias=True)
            serialized_payload = json.dumps(sanitized_payload, sort_keys=True)
            result = run_cohort_scan(
                session,
                merchant_id=dataset.merchant_id,
                window_from=window_from,
                window_to=window_to,
                provider=DeterministicCohortProvider(),
                batch_run_id=dataset.run_id,
            )
            ledger = session.scalar(
                select(LLMCall).where(LLMCall.purpose == "cohort_scan")
            )
            suppression = session.scalar(select(Suppression))
            unrelated_suppressed = int(
                session.scalar(
                    select(func.count())
                    .select_from(RecoveryCase)
                    .where(
                        RecoveryCase.entity_id.not_in(outage_ids),
                        RecoveryCase.state == CaseState.SUPPRESSED.value,
                    )
                )
                or 0
            )
            audit_kinds = set(
                session.scalars(
                    select(Event.kind)
                    .join(RecoveryCase, RecoveryCase.id == Event.case_id)
                    .where(RecoveryCase.entity_id.in_(outage_ids))
                )
            )
            evidence = candidates[0]
            accepted = {
                "observed_current": {
                    "attempts": evidence.attempts,
                    "failures": evidence.failures,
                    "failure_rate": evidence.observed_failure_rate,
                },
                "observed_baseline": {
                    "attempts": evidence.baseline_attempts,
                    "failures": evidence.baseline_failures,
                    "failure_rate": evidence.baseline_rate,
                },
                "evidence_slice_ids": [evidence.slice_id],
                "scope": suppression.scope if suppression is not None else None,
                "proposal_accepted": "AI_PROPOSED" in audit_kinds,
                "deterministic_validation_passed": "POLICY_VALIDATED" in audit_kinds,
                "scoped_consequence_opened": result.suppressions_opened == 1,
                "matching_cases_affected": result.cases_suppressed,
                "unrelated_cases_affected": unrelated_suppressed,
                "model_telemetry": {
                    "cost_paise": ledger.cost_paise if ledger is not None else None,
                    "latency_ms": ledger.latency_ms if ledger is not None else None,
                    "schema_ok": ledger.schema_ok if ledger is not None else False,
                },
                "audit_events": sorted(
                    audit_kinds
                    & {"AI_PROPOSED", "POLICY_VALIDATED", "SUPPRESSION_OPENED", "SUPPRESSED"}
                ),
                "aggregate_contains_pii_fields": any(
                    name in serialized_payload
                    for name in ("customer_id", "entity_id", "recipient", "email", "phone")
                ),
            }
    finally:
        accepted_engine.dispose()

    fallback_engine = _engine()
    try:
        with Session(fallback_engine, expire_on_commit=False) as session:
            persist_dataset(session, dataset)
            fallback = run_cohort_scan(
                session,
                merchant_id=dataset.merchant_id,
                window_from=window_from,
                window_to=window_to,
                provider=DisabledCohortProvider(),
                batch_run_id=dataset.run_id,
            )
            fallback_kinds = set(session.scalars(select(Event.kind)))
            fallback_suppressions = int(
                session.scalar(select(func.count()).select_from(Suppression)) or 0
            )
            disabled = {
                "status": fallback.status,
                "degraded": fallback.degraded,
                "suppressions_opened": fallback_suppressions,
                "safe_no_action_audited": {"AI_DEGRADED", "NO_ACTION"}.issubset(
                    fallback_kinds
                ),
            }
    finally:
        fallback_engine.dispose()

    checks = {
        "observed_denominators_present": (
            accepted["observed_current"]["attempts"] == 52
            and accepted["observed_baseline"]["attempts"] == 100
        ),
        "aggregate_payload_excludes_pii": not accepted["aggregate_contains_pii_fields"],
        "proposal_and_validation_audited": (
            accepted["proposal_accepted"] and accepted["deterministic_validation_passed"]
        ),
        "scope_is_hdfc_netbanking": accepted["scope"] == {
            "issuer": "HDFC",
            "method": "netbanking",
        },
        "only_matching_cases_affected": (
            accepted["matching_cases_affected"] == 47
            and accepted["unrelated_cases_affected"] == 0
        ),
        "model_telemetry_complete": (
            accepted["model_telemetry"]["schema_ok"] is True
            and accepted["model_telemetry"]["cost_paise"] > 0
            and accepted["model_telemetry"]["latency_ms"] >= 0
        ),
        "model_disabled_fails_safe": (
            disabled["degraded"]
            and disabled["suppressions_opened"] == 0
            and disabled["safe_no_action_audited"]
        ),
    }
    return {
        "schema_version": "2026-09-04",
        "data_provenance": "SIMULATED_END_TO_END",
        "label": "synthetic incident replay through production aggregation path",
        "passed": all(checks.values()),
        "accepted_incident": accepted,
        "model_disabled_fallback": disabled,
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/ai-acceptance/cohort-incident.json"),
    )
    args = parser.parse_args()
    artifact = capture()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    print(f"saved sanitized AI incident artifact to {args.output}")
    if not artifact["passed"]:
        failed = [name for name, passed in artifact["checks"].items() if not passed]
        print("failed checks: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
