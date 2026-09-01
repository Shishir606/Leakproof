from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from leakproof.diagnosis.tier2 import (
    CohortScanInput,
    CohortScanOutput,
    RawModelResponse,
    StructuredLLMClient,
    aggregate_cohort_window,
    qualified_slices,
    run_cohort_scan,
    strict_cohort_output_schema,
)
from leakproof.models.db import (
    Action,
    Event,
    LLMCall,
    Merchant,
    PaymentAttemptObservation,
    RecoveryCase,
    Suppression,
)
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


def test_aggregate_contract_contains_counts_and_dimensions_but_no_pii(session_factory, dataset):
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

    assert payload["totals"]["attempts"] == 52
    assert payload["totals"]["failures"] == 47
    assert payload["totals"]["observed_failure_rate"] == pytest.approx(47 / 52)
    assert payload["baseline"] == {
        "attempts_7d": 100,
        "failures_7d": 4,
        "failure_rate_7d": 0.04,
        "failure_rate_1h": pytest.approx(47 / 52),
    }
    assert payload["slices"][0]["dim"] == {"issuer": "HDFC", "method": "netbanking"}
    assert payload["slices"][0]["attempts"] == 52
    assert payload["slices"][0]["failures"] == 47
    assert payload["slices"][0]["baseline_attempts"] == 100
    assert payload["slices"][0]["baseline_failures"] == 4
    assert payload["slices"][0]["baseline_rate"] == 0.04
    assert payload["slices"][0]["observed_failure_rate"] == pytest.approx(47 / 52)
    assert payload["slices"][0]["failure_rate_change"] == pytest.approx(47 / 52 - 0.04)
    assert payload["slices"][0]["failure_rate_ratio"] == pytest.approx((47 / 52) / 0.04)
    assert payload["slices"][0]["top_reasons"] == {"gateway_technical_error": 47}
    assert payload["slices"][0]["slice_id"].startswith("slice_")
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
                Event.payload["evidence"]["simulation"]["scenario"].as_string() == "issuer_outage"
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
        non_outage_case = next(
            case
            for case in session.scalars(select(RecoveryCase).order_by(RecoveryCase.id))
            if case.id != outage_case.id
            and session.scalar(
                select(Event).where(
                    Event.case_id == case.id,
                    Event.kind == "DETECTED",
                    Event.payload["evidence"]["simulation"]["scenario"].as_string()
                    != "issuer_outage",
                )
            )
            is not None
        )
        session.add(
            Action(
                id="act_unrelated_pending",
                case_id=non_outage_case.id,
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
        assert session.get(Action, "act_unrelated_pending").status == "pending"
        assert session.get(RecoveryCase, non_outage_case.id).outcome != CaseOutcome.SUPPRESSED
        assert ledger.case_id is None
        assert ledger.purpose == "cohort_scan"
        assert ledger.prompt_version == "cohort_scan_v1"
        assert ledger.schema_ok is True
        assert ledger.cost_paise > 0
        assert (
            session.scalar(
                select(func.count()).select_from(Event).where(Event.kind == "SUPPRESSED")
            )
            == 47
        )
        first_kinds = list(
            session.scalars(
                select(Event.kind).where(Event.case_id == outage_cases[0].id).order_by(Event.seq)
            )
        )
        assert first_kinds[-4:] == [
            "AI_PROPOSED",
            "POLICY_VALIDATED",
            "SUPPRESSION_OPENED",
            "SUPPRESSED",
        ]

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


class _NoActionTransport:
    def complete(self, **_kwargs) -> RawModelResponse:
        return RawModelResponse(
            data={"anomalies": []},
            input_tokens=10,
            output_tokens=5,
            cost_paise=1,
            latency_ms=1,
        )


def test_qualified_incident_can_end_in_visible_no_action(session_factory, dataset):
    with session_factory() as session:
        persist_dataset(session, dataset)
        window_from, window_to = _outage_window(dataset)
        result = run_cohort_scan(
            session,
            merchant_id=dataset.merchant_id,
            window_from=window_from,
            window_to=window_to,
            client=StructuredLLMClient(_NoActionTransport()),
        )
        kinds = list(
            session.scalars(
                select(Event.kind).where(
                    Event.kind.in_(["AI_PROPOSED", "POLICY_VALIDATED", "NO_ACTION"])
                )
            )
        )

        assert result.qualified_slices == 1
        assert result.anomalies == 0
        assert result.suppressions_opened == 0
        assert kinds.count("AI_PROPOSED") == 47
        assert kinds.count("POLICY_VALIDATED") == 47
        assert kinds.count("NO_ACTION") == 47
        assert session.scalar(select(func.count()).select_from(Suppression)) == 0


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
        assert ledger.retries == 0
        assert ledger.input_tokens == 10
        assert ledger.output_tokens == 5
        assert ledger.cost_paise == 2
        degraded_kinds = list(
            session.scalars(select(Event.kind).where(Event.kind.in_(["AI_DEGRADED", "NO_ACTION"])))
        )
        assert degraded_kinds.count("AI_DEGRADED") == 47
        assert degraded_kinds.count("NO_ACTION") == 47


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


def test_live_factory_selects_openai_not_deterministic(monkeypatch):
    from leakproof.providers import factory
    from leakproof.providers.openai import OpenAICohortAnalysisProvider

    settings = SimpleNamespace(
        mode="live_demo",
        openai_api_key="test-key",
        luna_enabled=True,
        openai_model="gpt-5.6-luna",
        openai_usd_to_inr=100.0,
    )
    monkeypatch.setattr(factory, "get_settings", lambda: settings)
    factory.get_cohort_analysis_provider.cache_clear()
    try:
        provider = factory.get_cohort_analysis_provider()
        assert isinstance(provider, OpenAICohortAnalysisProvider)
        assert provider.__class__.__name__ != "DeterministicCohortProvider"
    finally:
        factory.get_cohort_analysis_provider.cache_clear()


def test_openai_cohort_schema_is_strict_and_null_scope_fields_are_removed():
    schema = strict_cohort_output_schema()
    anomaly_schema = schema["$defs"]["CohortAnomaly"]
    scope_schema = anomaly_schema["properties"]["scope"]

    assert schema["required"] == ["anomalies"]
    assert set(anomaly_schema["required"]) == set(anomaly_schema["properties"])
    assert scope_schema["additionalProperties"] is False
    assert set(scope_schema["required"]) == set(scope_schema["properties"])

    output = CohortScanOutput.model_validate(
        {
            "anomalies": [
                {
                    "pattern": "issuer_outage",
                    "evidence_slice_ids": ["slice_safe"],
                    "scope": {
                        "issuer": "HDFC",
                        "method": "netbanking",
                        "bin": None,
                        "checkout_step": None,
                        "checkout_version": None,
                        "payer_group": None,
                    },
                    "evidence": "47/52 observed failures",
                    "confidence": 0.9,
                    "recommended_action": "GLOBAL_SUPPRESS",
                    "ttl_minutes": 60,
                }
            ]
        }
    )
    assert output.anomalies[0].scope == {"issuer": "HDFC", "method": "netbanking"}


def test_failure_only_observations_without_baseline_are_insufficient(session_factory):
    now = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        session.add(Merchant(id="merchant_observed_only", name="Observed only", policy={}))
        session.add_all(
            PaymentAttemptObservation(
                merchant_id="merchant_observed_only",
                provider="razorpay",
                namespace="live",
                attempt_key=f"payment:failure_{index}",
                provider_event_key=f"event_{index}",
                observed_at=now + timedelta(seconds=index),
                outcome="failure",
                method="netbanking",
                issuer="HDFC",
                bin_bucket="unknown",
                checkout_step="payment_authorization",
                checkout_version="unknown",
                error_reason="gateway_technical_error",
                source="live_provider",
            )
            for index in range(25)
        )
        session.commit()

        result = run_cohort_scan(
            session,
            merchant_id="merchant_observed_only",
            window_from=now,
            window_to=now + timedelta(minutes=20),
        )

        assert result.status == "INSUFFICIENT_DATA"
        assert result.qualified_slices == 0
        assert session.scalar(select(func.count()).select_from(LLMCall)) == 0


def test_live_and_synthetic_attempt_namespaces_never_mix(session_factory, dataset):
    window_from, window_to = _outage_window(dataset)
    with session_factory() as session:
        persist_dataset(session, dataset)
        session.add(
            PaymentAttemptObservation(
                merchant_id=dataset.merchant_id,
                provider="razorpay",
                namespace="live",
                attempt_key="payment:live_isolated",
                provider_event_key="event_live_isolated",
                observed_at=window_from,
                outcome="failure",
                method="netbanking",
                issuer="HDFC",
                bin_bucket="unknown",
                checkout_step="payment_authorization",
                checkout_version="unknown",
                error_reason="gateway_technical_error",
                source="live_provider",
            )
        )
        session.commit()

        synthetic = aggregate_cohort_window(
            session,
            merchant_id=dataset.merchant_id,
            window_from=window_from,
            window_to=window_to,
            batch_run_id=dataset.run_id,
        )
        live = aggregate_cohort_window(
            session,
            merchant_id=dataset.merchant_id,
            window_from=window_from,
            window_to=window_to,
            batch_run_id="live",
        )

        assert synthetic.totals.attempts == 52
        assert synthetic.totals.failures == 47
        assert live.totals.attempts == 1
        assert live.totals.failures == 1


class _UnsafeProposalTransport:
    def __init__(self, *, scope: dict[str, str], ttl_minutes: int = 60):
        self.scope = scope
        self.ttl_minutes = ttl_minutes

    def complete(self, **kwargs) -> RawModelResponse:
        evidence_slice_id = kwargs["payload"]["slices"][0]["slice_id"]
        return RawModelResponse(
            data={
                "anomalies": [
                    {
                        "pattern": "issuer_outage",
                        "evidence_slice_ids": [evidence_slice_id],
                        "scope": self.scope,
                        "evidence": "47/52 failures",
                        "confidence": 0.95,
                        "recommended_action": "GLOBAL_SUPPRESS",
                        "ttl_minutes": self.ttl_minutes,
                    }
                ]
            },
            input_tokens=10,
            output_tokens=10,
            cost_paise=1,
            latency_ms=1,
        )


@pytest.mark.parametrize(
    ("scope", "ttl_minutes", "reason"),
    [
        (
            {"issuer": "INVENTED", "method": "netbanking"},
            60,
            "scope_expands_beyond_supplied_evidence",
        ),
        (
            {"method": "netbanking"},
            60,
            "scope_expands_beyond_supplied_evidence",
        ),
        ({"issuer": "HDFC", "method": "netbanking"}, 121, "ttl_exceeds_policy_maximum"),
    ],
)
def test_unsafe_ai_proposals_are_audited_and_rejected(
    session_factory, dataset, scope, ttl_minutes, reason
):
    with session_factory() as session:
        persist_dataset(session, dataset)
        window_from, window_to = _outage_window(dataset)
        result = run_cohort_scan(
            session,
            merchant_id=dataset.merchant_id,
            window_from=window_from,
            window_to=window_to,
            client=StructuredLLMClient(
                _UnsafeProposalTransport(scope=scope, ttl_minutes=ttl_minutes)
            ),
        )
        kinds = list(session.scalars(select(Event.kind).order_by(Event.id)))
        rejection = session.scalar(select(Event).where(Event.kind == "AI_PROPOSAL_REJECTED"))

        assert result.proposals_rejected == 1
        assert result.suppressions_opened == 0
        assert "AI_PROPOSED" in kinds
        assert "AI_PROPOSAL_REJECTED" in kinds
        assert "NO_ACTION" in kinds
        assert rejection.payload["reason"] == reason


def test_august_30_api_exposes_ledger_and_human_breaker_override(session_factory, client):
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
