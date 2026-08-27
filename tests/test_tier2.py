from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from leakproof.diagnosis.tier2 import (
    CohortScanInput,
    RawModelResponse,
    StructuredLLMClient,
    aggregate_cohort_window,
    qualified_slices,
    run_cohort_scan,
)
from leakproof.models.db import Action, Event, LLMCall, RecoveryCase, Suppression
from leakproof.models.domain import CaseOutcome, CaseState
from leakproof.simulator.generate import SimulationDataset, generate_dataset, load_parameters
from leakproof.simulator.seed import persist_dataset


@pytest.fixture(scope="module")
def dataset() -> SimulationDataset:
    return generate_dataset(load_parameters())


def _outage_window(dataset: SimulationDataset):
    signals = [item for item in dataset.signals if item.scenario == "issuer_outage"]
    return (
        min(item.occurred_at for item in signals),
        max(item.occurred_at for item in signals) + timedelta(seconds=1),
    )


def test_aggregate_contract_contains_counts_and_dimensions_but_no_pii(
    session_factory, dataset
):
    with session_factory() as session:
        persist_dataset(session, dataset)
        window_from, window_to = _outage_window(dataset)
        aggregate = aggregate_cohort_window(
            session,
            merchant_id=dataset.merchant_id,
            window_from=window_from,
            window_to=window_to,
        )
        payload = aggregate.model_dump(mode="json", by_alias=True)

    assert payload["totals"] == {"attempts": 57, "failures": 52}
    assert payload["slices"][0] == (
        {
            "dim": {"issuer": "HDFC", "method": "netbanking"},
            "attempts": 52,
            "failures": 47,
            "baseline_rate": 0.043,
            "top_reasons": {"gateway_technical_error": 47},
        }
    )
    serialized = str(payload)
    assert "customer_id" not in serialized
    assert "entity_id" not in serialized
    assert len(qualified_slices(aggregate)) == 1


def test_outage_opens_one_breaker_and_suppresses_all_47_cases(session_factory, dataset):
    with session_factory() as session:
        persist_dataset(session, dataset)
        outage_case = session.scalar(
            select(RecoveryCase)
            .join(Event, Event.case_id == RecoveryCase.id)
            .where(
                Event.payload["evidence"]["simulation"]["scenario"].as_string()
                == "issuer_outage"
            )
        )
        session.add(
            Action(
                id="act_outage_pending",
                case_id=outage_case.id,
                step_index=0,
                action_type="silent_retry",
                scheduled_for=dataset.as_of,
                status="pending",
                cost_paise=0,
            )
        )
        session.commit()
        window_from, window_to = _outage_window(dataset)

        result = run_cohort_scan(
            session,
            merchant_id=dataset.merchant_id,
            window_from=window_from,
            window_to=window_to,
        )
        outage_cases = list(
            session.scalars(
                select(RecoveryCase)
                .join(Event, Event.case_id == RecoveryCase.id)
                .where(
                    Event.kind == "DETECTED",
                    Event.payload["evidence"]["simulation"]["scenario"].as_string()
                    == "issuer_outage",
                )
            )
        )
        ledger = session.scalar(select(LLMCall))
        suppression = session.scalar(select(Suppression))

        assert result.qualified_slices == 1
        assert result.suppressions_opened == 1
        assert result.cases_suppressed == 47
        assert result.degraded is False
        assert len(outage_cases) == 47
        assert all(item.state == CaseState.SUPPRESSED for item in outage_cases)
        assert all(item.outcome == CaseOutcome.SUPPRESSED for item in outage_cases)
        assert suppression.scope == {"issuer": "HDFC", "method": "netbanking"}
        assert suppression.pattern == "issuer_outage"
        assert session.get(Action, "act_outage_pending").status == "cancelled"
        assert ledger.case_id is None
        assert ledger.purpose == "cohort_scan"
        assert ledger.prompt_version == "cohort_scan_v1"
        assert ledger.schema_ok is True
        assert ledger.cost_paise > 0
        assert session.scalar(
            select(func.count()).select_from(Event).where(Event.kind == "SUPPRESSED")
        ) == 47

        repeated = run_cohort_scan(
            session,
            merchant_id=dataset.merchant_id,
            window_from=window_from,
            window_to=window_to,
        )
        assert repeated.cases_suppressed == 0
        assert repeated.suppressions_opened == 0
        assert session.scalar(select(func.count()).select_from(Suppression)) == 1


class _MalformedTransport:
    def complete(self, **_kwargs) -> RawModelResponse:
        return RawModelResponse(
            data={"anomalies": [{"invented": True}]},
            input_tokens=10,
            output_tokens=5,
            cost_paise=2,
            latency_ms=3,
        )


def test_schema_failure_retries_once_logs_and_fails_safe(session_factory, dataset):
    with session_factory() as session:
        persist_dataset(session, dataset)
        window_from, window_to = _outage_window(dataset)
        result = run_cohort_scan(
            session,
            merchant_id=dataset.merchant_id,
            window_from=window_from,
            window_to=window_to,
            client=StructuredLLMClient(_MalformedTransport()),
        )
        ledger = session.scalar(select(LLMCall))

        assert result.degraded is True
        assert result.cases_suppressed == 0
        assert session.scalar(select(func.count()).select_from(Suppression)) == 0
        assert ledger.schema_ok is False
        assert ledger.retries == 1
        assert ledger.input_tokens == 20
        assert ledger.output_tokens == 10
        assert ledger.cost_paise == 4


def test_near_miss_is_not_sent_to_the_model():
    scan = CohortScanInput.model_validate(
        {
            "window": {"from": "2026-08-30T10:00:00Z", "to": "2026-08-30T10:20:00Z"},
            "totals": {"attempts": 52, "failures": 6},
            "baseline": {"failure_rate_7d": 0.043, "failure_rate_1h": 0.04},
            "slices": [
                {
                    "dim": {"issuer": "HDFC", "method": "netbanking"},
                    "attempts": 52,
                    "failures": 6,
                    "baseline_rate": 0.043,
                    "top_reasons": {"gateway_technical_error": 5, "insufficient_funds": 1},
                }
            ],
            "open_suppressions": [],
        }
    )

    assert qualified_slices(scan) == []


def test_august_30_api_exposes_ledger_and_human_breaker_override(
    session_factory, client
):
    now = datetime.now(UTC)
    with session_factory() as session:
        session.add_all(
            [
                LLMCall(
                    purpose="cohort_scan",
                    model="small",
                    prompt_version="cohort_scan_v1",
                    input_tokens=100,
                    output_tokens=25,
                    cost_paise=3,
                    latency_ms=20,
                    schema_ok=True,
                    retries=0,
                ),
                LLMCall(
                    purpose="cohort_scan",
                    model="large",
                    prompt_version="cohort_scan_v1",
                    input_tokens=200,
                    output_tokens=10,
                    cost_paise=7,
                    latency_ms=40,
                    schema_ok=False,
                    retries=1,
                ),
            ]
        )
        suppression = Suppression(
            merchant_id="merchant_api",
            scope={"issuer": "HDFC", "method": "netbanking"},
            pattern="issuer_outage",
            reason="47/52 failures",
            opened_at=now,
            expires_at=now + timedelta(hours=1),
            opened_by="tier2",
        )
        session.add(suppression)
        session.commit()
        suppression_id = suppression.id

    costs = client.get("/costs")
    assert costs.status_code == 200
    assert costs.json() == {
        "calls": 2,
        "input_tokens": 300,
        "output_tokens": 35,
        "cost_paise": 10,
        "latency_ms": 60,
        "schema_ok_calls": 1,
        "schema_failed_calls": 1,
        "by_purpose": [{"purpose": "cohort_scan", "calls": 2, "cost_paise": 10}],
    }
    open_response = client.get("/suppressions")
    assert open_response.status_code == 200
    assert [item["id"] for item in open_response.json()] == [suppression_id]

    closed = client.post(f"/suppressions/{suppression_id}/close")
    assert closed.status_code == 200
    assert client.get("/suppressions").json() == []
