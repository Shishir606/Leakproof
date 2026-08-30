from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leakproof.audit.timeline import append_event
from leakproof.config import Settings, get_policy_config
from leakproof.demo.contracts import CaseInsight
from leakproof.models.db import (
    CaseInsightRecord,
    DemoSession,
    Diagnosis,
    LLMCall,
    ProviderCall,
    RecoveryCase,
)
from leakproof.providers import CaseInsightProvider, CaseInsightRequest, ProviderError
from leakproof.providers.openai import CASE_INSIGHT_PROMPT_VERSION

_SAFE_PROVIDER_FIELDS = frozenset({"error_code", "error_reason", "error_source", "error_step"})
_SAFE_CLASSIFICATION = re.compile(r"^[a-zA-Z0-9_.-]{1,100}$")
_SAFE_PAYMENT_METHODS = frozenset(
    {"card", "upi", "netbanking", "wallet", "emi", "paylater", "bank_transfer"}
)


def build_case_insight_request(case: RecoveryCase, diagnosis: Diagnosis) -> CaseInsightRequest:
    """Build the complete provider payload from an explicit non-PII allowlist."""
    evidence = diagnosis.evidence or {}
    fields: dict[str, str | int | float | bool | None] = {}
    for key in _SAFE_PROVIDER_FIELDS:
        value = evidence.get(key)
        if isinstance(value, bool | int | float):
            fields[key] = value
        elif (
            isinstance(value, str)
            and _SAFE_CLASSIFICATION.fullmatch(value)
            and not re.search(r"\d{7,}", value)
            and not value.casefold().startswith(("pay_", "order_", "cust_", "demo_"))
        ):
            fields[key] = value
    raw_method = evidence.get("method")
    method = raw_method.casefold() if isinstance(raw_method, str) else None
    if method not in _SAFE_PAYMENT_METHODS:
        method = "other" if method else None
    return CaseInsightRequest(
        failure_class=diagnosis.failure_class,
        payment_method=method,
        amount_band=case.amount_band,
        aggregate_provider_fields=fields,
    )


def _fallback_insight(request: CaseInsightRequest, confidence: float) -> CaseInsight:
    cause = request.failure_class.replace("_", " ").lower()
    evidence = [f"Deterministic Tier 1 classification: {request.failure_class}"]
    for key, value in sorted(request.aggregate_provider_fields.items()):
        evidence.append(f"{key.replace('_', ' ').title()}: {value}")
    return CaseInsight(
        summary="The payment was not completed and still needs customer authorization.",
        probable_cause=f"The deterministic rules classified this as {cause}.",
        evidence=evidence[:8],
        recommended_next_step="Reopen the original Checkout order using the signed recovery link.",
        confidence=max(0.0, min(1.0, confidence)),
    )


def _demo_for_case(session: Session, case: RecoveryCase) -> DemoSession | None:
    return session.scalar(
        select(DemoSession).where(
            DemoSession.merchant_id == case.merchant_id,
            DemoSession.customer_id == case.customer_id,
        )
    )


def mark_case_insight_pending(session: Session, case_id: str) -> CaseInsightRecord:
    existing = session.get(CaseInsightRecord, case_id)
    if existing is not None:
        return existing
    record = CaseInsightRecord(
        case_id=case_id,
        evidence=[],
        status="pending",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(record)
    session.flush()
    return record


def _finish(
    session: Session,
    *,
    case: RecoveryCase,
    diagnosis: Diagnosis,
    record: CaseInsightRecord,
    insight: CaseInsight,
    status: str,
    fallback_reason: str | None,
    model: str,
    request_id: str | None,
    input_tokens: int,
    output_tokens: int,
    cost_paise: int,
    latency_ms: int,
    attempts: int,
    schema_ok: bool,
    provider_status: str,
    request: CaseInsightRequest,
) -> CaseInsightRecord:
    record.summary = insight.summary
    record.probable_cause = insight.probable_cause
    record.evidence = insight.evidence
    record.recommended_next_step = insight.recommended_next_step
    record.confidence = insight.confidence
    record.status = status
    record.fallback_reason = fallback_reason
    record.updated_at = datetime.now(UTC)
    session.add(
        LLMCall(
            case_id=case.id,
            purpose="case_insight",
            model=model,
            prompt_version=CASE_INSIGHT_PROMPT_VERSION,
            input_tokens=max(0, input_tokens),
            output_tokens=max(0, output_tokens),
            cost_paise=max(0, cost_paise),
            latency_ms=max(0, latency_ms),
            schema_ok=schema_ok,
            retries=max(0, attempts - 1),
        )
    )
    demo = _demo_for_case(session, case)
    session.add(
        ProviderCall(
            session_id=demo.id if demo else None,
            case_id=case.id,
            provider="openai",
            operation="case_insight",
            request_id=request_id,
            safe_response_metadata={
                "prompt_version": CASE_INSIGHT_PROMPT_VERSION,
                "failure_class": request.failure_class,
                "payment_method": request.payment_method,
                "amount_band": request.amount_band,
                "aggregate_field_names": sorted(request.aggregate_provider_fields),
                "input_tokens": max(0, input_tokens),
                "output_tokens": max(0, output_tokens),
                "cost_paise": max(0, cost_paise),
                "schema_ok": schema_ok,
            },
            latency_ms=max(0, latency_ms),
            attempt_number=max(1, attempts),
            status=provider_status,
            error_class=fallback_reason,
        )
    )
    append_event(
        session,
        case,
        kind="CASE_INSIGHT_READY",
        payload={
            "status": status,
            "fallback_reason": fallback_reason,
            "prompt_version": CASE_INSIGHT_PROMPT_VERSION,
            "insight": insight.model_dump(mode="json"),
            "request_id": request_id,
            "cost_paise": max(0, cost_paise),
        },
        actor="luna" if status == "succeeded" else "deterministic_fallback",
    )
    session.commit()
    return record


def _fallback_reason(error_class: str) -> str:
    mapping = {
        "timeout": "timeout",
        "quota_exhausted": "quota_exhausted",
        "invalid_schema": "invalid_schema",
        "budget_exhausted": "budget_exhausted",
    }
    return mapping.get(error_class, "provider_unavailable")


def generate_case_insight(
    session: Session,
    case_id: str,
    *,
    provider: CaseInsightProvider,
    settings: Settings,
) -> CaseInsightRecord:
    """Persist one Luna insight or deterministic fallback without affecting recovery."""
    case = session.get(RecoveryCase, case_id)
    diagnosis = session.get(Diagnosis, case_id)
    if case is None:
        raise LookupError(case_id)
    if diagnosis is None:
        raise ValueError(f"case {case_id} must be diagnosed before requesting an insight")
    record = session.scalar(
        select(CaseInsightRecord)
        .where(CaseInsightRecord.case_id == case_id)
        .with_for_update()
    )
    if record is None:
        record = mark_case_insight_pending(session, case_id)
    if record.status in {"succeeded", "fallback"}:
        return record

    request = build_case_insight_request(case, diagnosis)
    if not settings.luna_enabled:
        return _finish(
            session,
            case=case,
            diagnosis=diagnosis,
            record=record,
            insight=_fallback_insight(request, float(diagnosis.confidence)),
            status="fallback",
            fallback_reason="disabled",
            model="disabled",
            request_id=None,
            input_tokens=0,
            output_tokens=0,
            cost_paise=0,
            latency_ms=0,
            attempts=1,
            schema_ok=False,
            provider_status="disabled",
            request=request,
        )
    config = get_policy_config(str(settings.config_dir))
    spent = int(
        session.scalar(
            select(func.coalesce(func.sum(LLMCall.cost_paise), 0)).where(
                LLMCall.case_id == case.id,
                LLMCall.purpose == "case_insight",
            )
        )
        or 0
    )
    if spent >= config.models.budgets.per_case_paise:
        return _finish(
            session,
            case=case,
            diagnosis=diagnosis,
            record=record,
            insight=_fallback_insight(request, float(diagnosis.confidence)),
            status="fallback",
            fallback_reason="budget_exhausted",
            model="budget_guard",
            request_id=None,
            input_tokens=0,
            output_tokens=0,
            cost_paise=0,
            latency_ms=0,
            attempts=1,
            schema_ok=False,
            provider_status="budget_blocked",
            request=request,
        )

    try:
        result = provider.explain_case(request)
    except ProviderError as exc:
        reason = _fallback_reason(exc.error_class)
        return _finish(
            session,
            case=case,
            diagnosis=diagnosis,
            record=record,
            insight=_fallback_insight(request, float(diagnosis.confidence)),
            status="fallback",
            fallback_reason=reason,
            model=settings.openai_model,
            request_id=exc.request_id,
            input_tokens=exc.input_tokens,
            output_tokens=exc.output_tokens,
            cost_paise=exc.cost_paise,
            latency_ms=exc.latency_ms,
            attempts=exc.attempts,
            schema_ok=False,
            provider_status="failed",
            request=request,
        )

    return _finish(
        session,
        case=case,
        diagnosis=diagnosis,
        record=record,
        insight=result.insight,
        status="succeeded",
        fallback_reason=None,
        model=settings.openai_model,
        request_id=result.request_id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_paise=result.cost_paise,
        latency_ms=result.latency_ms,
        attempts=result.attempts,
        schema_ok=True,
        provider_status="succeeded",
        request=request,
    )
