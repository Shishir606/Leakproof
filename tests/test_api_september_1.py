from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx2
from sqlalchemy import func, select

from leakproof.config import Settings
from leakproof.demo import DemoSessionCreateRequest
from leakproof.demo.contracts import CaseInsight, DemoSessionState
from leakproof.demo.insights import build_case_insight_request, generate_case_insight
from leakproof.demo.projection import get_demo_session_projection
from leakproof.demo.rate_limit import InMemoryRateLimiter
from leakproof.demo.service import create_demo_session
from leakproof.diagnosis import diagnose_case, refresh_payment_diagnosis
from leakproof.models.db import CaseInsightRecord, DemoSession, Event, LLMCall, ProviderCall
from leakproof.models.domain import Arm, LeakType
from leakproof.providers import CaseInsightRequest, OpenAICaseInsightProvider, ProviderError
from leakproof.providers.fakes import FakeCaseInsightProvider, FakePaymentProvider
from leakproof.services import NormalizedSignal, record_signal

NOW = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)


def settings() -> Settings:
    return Settings(
        _env_file=None,
        mode="simulation",
        default_merchant_id="merchant_luna_demo",
        recovery_token_secret="september-1-luna-secret-long-enough",
        razorpay_key_id="rzp_test_september_1",
    )


def create_case(session):
    config = settings()
    created = create_demo_session(
        session,
        DemoSessionCreateRequest(recipient="reviewer@example.com"),
        client_ip="203.0.113.91",
        provider=FakePaymentProvider(),
        limiter=InMemoryRateLimiter(),
        settings=config,
        now=NOW,
    )
    demo = session.get(DemoSession, created.session_id)
    case, _ = record_signal(
        session,
        NormalizedSignal(
            merchant_id=demo.merchant_id,
            customer_id=demo.customer_id,
            leak_type=LeakType.PAYMENT_FAILURE,
            entity_type="payment",
            entity_id="pay_private_identifier",
            entity_root_id=demo.razorpay_order_id,
            amount_at_risk=demo.amount_paise,
            currency=demo.currency,
            evidence={
                "source": "razorpay_webhook",
                "session_id": demo.id,
                "method": "card",
                "error_source": "bank",
                "error_step": "payment_authorization",
                "error_reason": "gateway_technical_error",
                "error_code": "9999999999",
                "error_description": "reviewer@example.com +91 99999 99999",
                "customer_name": "Private Person",
            },
            occurred_at=NOW + timedelta(seconds=5),
            dedupe_key_override=f"live:{demo.id}:{demo.razorpay_order_id}",
            arm_override=Arm.TREATMENT,
        ),
    )
    diagnosis = diagnose_case(session, case.id)
    demo.state = DemoSessionState.AT_RISK.value
    session.commit()
    return created, case, diagnosis, config


def test_openai_responses_adapter_retries_invalid_schema_and_uses_safe_bounded_contract():
    requests: list[dict] = []
    valid = CaseInsight(
        summary="The issuer did not complete authorization.",
        probable_cause="Temporary issuer or gateway interruption.",
        evidence=["Tier 1 class: TRANSIENT"],
        recommended_next_step="Reopen Checkout for a customer-authorized attempt.",
        confidence=0.84,
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        output = "not-json" if len(requests) == 1 else valid.model_dump_json()
        return httpx2.Response(
            200,
            headers={"x-request-id": f"req_{len(requests)}"},
            json={
                "id": f"resp_{len(requests)}",
                "status": "completed",
                "usage": {"input_tokens": 50, "output_tokens": 25},
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": output}],
                    }
                ],
            },
        )

    provider = OpenAICaseInsightProvider(
        "test-key",
        client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        sleep=lambda _: None,
    )
    result = provider.explain_case(
        CaseInsightRequest(
            failure_class="TRANSIENT",
            payment_method="card",
            amount_band="LOW",
            aggregate_provider_fields={"error_source": "bank"},
        )
    )

    assert result.insight == valid
    assert result.attempts == 2
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.cost_paise > 0
    assert len(requests) == 2
    assert requests[0]["model"] == "gpt-5.6-luna"
    assert requests[0]["reasoning"] == {"effort": "low"}
    assert requests[0]["store"] is False
    assert requests[0]["tools"] == []
    assert requests[0]["max_output_tokens"] == 600
    assert requests[0]["text"]["format"]["type"] == "json_schema"
    assert requests[0]["text"]["format"]["strict"] is True
    assert "email" not in json.dumps(requests[0]).casefold()


def test_session_projection_route_returns_the_live_contract(client):
    created = client.post("/demo/sessions", json={})
    assert created.status_code == 201
    payload = created.json()

    response = client.get(
        f"/demo/sessions/{payload['session_id']}",
        headers={"x-leakproof-session-token": payload["session_token"]},
    )

    assert response.status_code == 200
    assert response.json()["session_id"] == payload["session_id"]
    assert response.json()["case"] is None
    assert response.json()["metrics"]["luna_cost_paise"] == 0


def test_case_insight_builder_excludes_pii_and_persists_success_in_projection(session_factory):
    with session_factory() as session:
        created, case, diagnosis, config = create_case(session)
        request = build_case_insight_request(case, diagnosis)
        serialized = json.dumps(request.__dict__, sort_keys=True)
        assert request.aggregate_provider_fields == {
            "error_reason": "gateway_technical_error",
            "error_source": "bank",
            "error_step": "payment_authorization",
        }
        for prohibited in (
            "reviewer@example.com",
            "99999",
            "Private Person",
            created.session_id,
            created.razorpay_order_id,
            "pay_private_identifier",
        ):
            assert prohibited not in serialized

        expected = CaseInsight(
            summary="Authorization did not complete.",
            probable_cause="Temporary bank interruption.",
            evidence=["Bank classified the failure as temporary."],
            recommended_next_step="Retry through the original Checkout order.",
            confidence=0.83,
        )
        record = generate_case_insight(
            session,
            case.id,
            provider=FakeCaseInsightProvider(result=expected),
            settings=config,
        )
        projection = get_demo_session_projection(
            session,
            created.session_id,
            session_token=created.session_token,
            settings=config,
            now=NOW + timedelta(minutes=1),
        )

        assert record.status == "succeeded"
        assert projection.case.insight == expected
        assert projection.case.insight_status == "succeeded"
        assert any(item.kind == "CASE_INSIGHT_READY" for item in projection.timeline)
        assert any(item.provider == "openai" for item in projection.provider_statuses)
        ledger = session.scalar(
            select(LLMCall).where(LLMCall.case_id == case.id, LLMCall.purpose == "case_insight")
        )
        provider_call = session.scalar(
            select(ProviderCall).where(
                ProviderCall.case_id == case.id,
                ProviderCall.provider == "openai",
            )
        )
        assert ledger.schema_ok is True
        assert ledger.prompt_version == "case_insight_v1"
        assert ledger.cost_paise == 1
        assert provider_call.request_id == "resp_fake_1"


def test_timeout_persists_deterministic_fallback_without_blocking_recovery(session_factory):
    with session_factory() as session:
        created, case, _, config = create_case(session)
        provider = FakeCaseInsightProvider(
            failure=ProviderError(
                provider="openai",
                operation="case_insight",
                error_class="timeout",
                retryable=True,
                message="timed out",
                latency_ms=8_000,
                attempts=2,
            )
        )
        record = generate_case_insight(
            session,
            case.id,
            provider=provider,
            settings=config,
        )
        projection = get_demo_session_projection(
            session,
            created.session_id,
            session_token=created.session_token,
            settings=config,
            now=NOW + timedelta(minutes=1),
        )

        assert record.status == "fallback"
        assert record.fallback_reason == "timeout"
        assert projection.case.insight_status == "fallback"
        assert projection.recovery_url_available is True
        assert projection.case.insight is not None
        assert session.scalar(
            select(func.count()).select_from(Event).where(
                Event.case_id == case.id,
                Event.kind == "CASE_INSIGHT_READY",
            )
        ) == 1


def test_case_budget_stop_skips_provider_and_records_fallback(session_factory):
    with session_factory() as session:
        _, case, _, config = create_case(session)
        session.add(
            LLMCall(
                case_id=case.id,
                purpose="case_insight",
                model="gpt-5.6-luna",
                prompt_version="case_insight_v0",
                input_tokens=1,
                output_tokens=1,
                cost_paise=200,
                latency_ms=1,
                schema_ok=True,
                retries=0,
            )
        )
        session.commit()
        provider = FakeCaseInsightProvider()

        record = generate_case_insight(
            session,
            case.id,
            provider=provider,
            settings=config,
        )

        assert provider.calls == []
        assert record.status == "fallback"
        assert record.fallback_reason == "budget_exhausted"
        assert session.scalar(select(func.count()).select_from(CaseInsightRecord)) == 1


def test_new_authoritative_failure_refreshes_a_prior_insight(session_factory):
    with session_factory() as session:
        _, case, diagnosis, config = create_case(session)
        generate_case_insight(
            session,
            case.id,
            provider=FakeCaseInsightProvider(),
            settings=config,
        )

        refreshed = refresh_payment_diagnosis(
            session,
            case,
            {"method": "upi", "error_reason": "insufficient_funds"},
        )
        record = session.get(CaseInsightRecord, case.id)

        assert diagnosis.failure_class == "TIMING"
        assert refreshed.failure_class == "TIMING"
        assert record.status == "pending"
        assert record.summary is None
        provider = FakeCaseInsightProvider()
        generate_case_insight(session, case.id, provider=provider, settings=config)
        assert provider.calls[0].failure_class == "TIMING"
