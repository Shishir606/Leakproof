from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leakproof.audit.timeline import append_event
from leakproof.config import ModelTarget, get_policy_config
from leakproof.models.db import Action, Event, LLMCall, RecoveryCase, Suppression
from leakproof.models.domain import CaseOutcome, CaseState

PROMPT_VERSION = "cohort_scan_v1"
BANK_OR_GATEWAY_REASONS = {
    "bank_unavailable",
    "gateway_technical_error",
    "issuer_down",
    "payment_method_down",
    "request_timed_out",
}


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class CohortWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    from_: datetime = Field(alias="from")
    to: datetime

    @field_validator("from_", "to")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _aware(value)


class CohortTotals(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempts: int = Field(ge=0)
    failures: int = Field(ge=0)


class CohortBaseline(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    failure_rate_7d: float = Field(ge=0, le=1)
    failure_rate_1h: float = Field(ge=0, le=1)


class CohortSlice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dim: dict[str, str]
    attempts: int = Field(ge=0)
    failures: int = Field(ge=0)
    baseline_rate: float = Field(ge=0, le=1)
    top_reasons: dict[str, int]

    @field_validator("dim")
    @classmethod
    def aggregate_dimensions_only(cls, value: dict[str, str]) -> dict[str, str]:
        allowed = {"issuer", "method", "bin", "checkout_step", "payer_group"}
        if not value or not set(value).issubset(allowed):
            raise ValueError("cohort dimensions must be non-PII aggregate fields")
        return value


class CohortScanInput(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    window: CohortWindow
    totals: CohortTotals
    baseline: CohortBaseline
    slices: list[CohortSlice]
    open_suppressions: list[dict[str, Any]] = Field(default_factory=list)


class CohortAnomaly(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pattern: Literal[
        "issuer_outage",
        "bin_rule_change",
        "method_degradation",
        "checkout_regression",
        "payer_cluster",
    ]
    scope: dict[str, str]
    evidence: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)
    recommended_action: Literal["GLOBAL_SUPPRESS", "ALERT_MERCHANT", "NO_ACTION"]
    ttl_minutes: int = Field(ge=1, le=24 * 60)

    @field_validator("scope")
    @classmethod
    def supported_scope(cls, value: dict[str, str]) -> dict[str, str]:
        return CohortSlice(dim=value, attempts=0, failures=0, baseline_rate=0, top_reasons={}).dim


class CohortScanOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    anomalies: list[CohortAnomaly] = Field(default_factory=list)


@dataclass(frozen=True)
class RawModelResponse:
    data: Any
    input_tokens: int
    output_tokens: int
    cost_paise: int
    latency_ms: int


class CohortModelTransport(Protocol):
    def complete(
        self,
        *,
        system_prompt: str,
        payload: dict[str, Any],
        model: str,
        max_tokens: int | None,
    ) -> RawModelResponse: ...


class DeterministicCohortTransport:
    """Offline structured-model substitute used only by simulation and tests."""

    def complete(
        self,
        *,
        system_prompt: str,
        payload: dict[str, Any],
        model: str,
        max_tokens: int | None,
    ) -> RawModelResponse:
        del system_prompt, max_tokens
        started = time.perf_counter()
        anomalies: list[dict[str, Any]] = []
        for item in payload.get("slices", []):
            attempts = int(item["attempts"])
            failures = int(item["failures"])
            baseline = float(item["baseline_rate"])
            reasons = item.get("top_reasons", {})
            bank_errors = sum(int(reasons.get(reason, 0)) for reason in BANK_OR_GATEWAY_REASONS)
            failure_rate = failures / attempts if attempts else 0
            bank_share = bank_errors / failures if failures else 0
            if not (
                attempts >= 20
                and failure_rate >= 3 * baseline
                and bank_share >= 0.80
            ):
                continue
            scope = dict(item["dim"])
            if "issuer" in scope and "method" in scope:
                pattern = "issuer_outage"
            elif "bin" in scope:
                pattern = "bin_rule_change"
            elif "method" in scope:
                pattern = "method_degradation"
            elif "checkout_step" in scope:
                pattern = "checkout_regression"
            else:
                pattern = "payer_cluster"
            confidence = min(0.99, 0.80 + min(0.15, (failure_rate - baseline) / 4))
            anomalies.append(
                {
                    "pattern": pattern,
                    "scope": scope,
                    "evidence": (
                        f"{failures}/{attempts} failures vs baseline {baseline:.1%}; "
                        f"{bank_errors}/{failures} bank/gateway errors"
                    ),
                    "confidence": round(confidence, 3),
                    "recommended_action": "GLOBAL_SUPPRESS",
                    "ttl_minutes": 60,
                }
            )
        serialized_input = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        result = {"anomalies": anomalies}
        serialized_output = json.dumps(result, sort_keys=True, separators=(",", ":"))
        input_tokens = max(1, len(serialized_input) // 4)
        output_tokens = max(1, len(serialized_output) // 4)
        return RawModelResponse(
            data=result,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_paise=max(1, (input_tokens + output_tokens + 99) // 100),
            latency_ms=max(1, round((time.perf_counter() - started) * 1000)),
        )


class StructuredOutputError(RuntimeError):
    pass


OutputModel = TypeVar("OutputModel", bound=BaseModel)


class StructuredLLMClient:
    def __init__(self, transport: CohortModelTransport | None = None) -> None:
        self.transport = transport or DeterministicCohortTransport()

    def call(
        self,
        session: Session,
        *,
        purpose: str,
        payload: dict[str, Any],
        schema: type[OutputModel],
        case_id: str | None = None,
    ) -> tuple[OutputModel, LLMCall]:
        config = get_policy_config()
        route = config.models.routes[purpose]
        spent = int(session.scalar(select(func.coalesce(func.sum(LLMCall.cost_paise), 0))) or 0)
        if spent >= config.models.budgets.per_batch_paise:
            record = LLMCall(
                case_id=case_id,
                purpose=purpose,
                model="budget_guard",
                prompt_version=PROMPT_VERSION,
                input_tokens=0,
                output_tokens=0,
                cost_paise=0,
                latency_ms=0,
                schema_ok=False,
                retries=0,
            )
            session.add(record)
            session.flush()
            raise StructuredOutputError("LLM batch budget exhausted")

        system_prompt = (
            "Analyze only the aggregate counts supplied. Report only patterns supported by "
            "those numbers, quote the counts used in evidence, never invent scope or TTL, and "
            "return NO_ACTION when a deviation is within noise. Return the required JSON schema."
        )
        targets: list[ModelTarget] = [route.primary]
        if route.escalate_to is not None:
            targets.append(route.escalate_to)
        else:
            targets.append(route.primary)
        totals = {"input_tokens": 0, "output_tokens": 0, "cost_paise": 0, "latency_ms": 0}
        parsed: OutputModel | None = None
        last_error: Exception | None = None
        used_model = targets[0].model
        retries = 0
        for index, target in enumerate(targets[:2]):
            used_model = target.model
            try:
                raw = self.transport.complete(
                    system_prompt=system_prompt,
                    payload=payload,
                    model=target.model,
                    max_tokens=target.max_tokens,
                )
            except Exception as exc:
                last_error = exc
                retries = index + 1
                continue
            for key in totals:
                totals[key] += int(getattr(raw, key))
            try:
                parsed = schema.model_validate(raw.data)
            except ValidationError as exc:
                last_error = exc
                retries = index + 1
                continue
            retries = index
            break

        record = LLMCall(
            case_id=case_id,
            purpose=purpose,
            model=used_model,
            prompt_version=PROMPT_VERSION,
            input_tokens=totals["input_tokens"],
            output_tokens=totals["output_tokens"],
            cost_paise=totals["cost_paise"],
            latency_ms=totals["latency_ms"],
            schema_ok=parsed is not None,
            retries=min(retries, 1),
        )
        session.add(record)
        session.flush()
        if parsed is None:
            raise StructuredOutputError(
                "model returned invalid structured output twice"
            ) from last_error
        return parsed, record


def aggregate_cohort_window(
    session: Session,
    *,
    merchant_id: str,
    window_from: datetime,
    window_to: datetime,
) -> CohortScanInput:
    """Aggregate failure events without placing customer or entity identifiers in the payload."""
    start, end = _aware(window_from), _aware(window_to)
    if end <= start:
        raise ValueError("cohort window end must be after its start")
    rows = session.execute(
        select(Event, RecoveryCase)
        .join(RecoveryCase, RecoveryCase.id == Event.case_id)
        .where(
            RecoveryCase.merchant_id == merchant_id,
            Event.kind == "DETECTED",
            RecoveryCase.detected_at >= start,
            RecoveryCase.detected_at < end,
        )
    ).all()
    grouped: dict[tuple[tuple[str, str], ...], list[dict[str, Any]]] = defaultdict(list)
    for event, _case in rows:
        evidence = dict(event.payload.get("evidence", {}))
        dimensions = {
            key: str(evidence[key])
            for key in ("issuer", "method", "bin", "checkout_step", "payer_group")
            if evidence.get(key) is not None
        }
        if dimensions:
            grouped[tuple(sorted(dimensions.items()))].append(evidence)

    slices: list[CohortSlice] = []
    for dimensions, evidence_rows in sorted(grouped.items()):
        failures = len(evidence_rows)
        declared_rate = max(
            (float(item.get("cohort_failure_rate", 0)) for item in evidence_rows),
            default=0,
        )
        attempts = max(failures, round(failures / declared_rate)) if declared_rate else failures
        baseline_rate = max(
            (float(item.get("baseline_failure_rate", 0.043)) for item in evidence_rows),
            default=0.043,
        )
        reasons = Counter(
            str(item["error_reason"])
            for item in evidence_rows
            if item.get("error_reason") is not None
        )
        slices.append(
            CohortSlice(
                dim=dict(dimensions),
                attempts=attempts,
                failures=failures,
                baseline_rate=baseline_rate,
                top_reasons=dict(reasons.most_common(5)),
            )
        )
    open_suppressions = [
        {"id": item.id, "scope": item.scope, "pattern": item.pattern, "expires_at": item.expires_at}
        for item in session.scalars(
            select(Suppression).where(
                Suppression.merchant_id == merchant_id,
                Suppression.expires_at > end,
            )
        )
    ]
    total_attempts = sum(item.attempts for item in slices)
    total_failures = sum(item.failures for item in slices)
    baseline = max((item.baseline_rate for item in slices), default=0.043)
    return CohortScanInput(
        window={"from": start, "to": end},
        totals={"attempts": total_attempts, "failures": total_failures},
        baseline={"failure_rate_7d": baseline, "failure_rate_1h": baseline},
        slices=slices,
        open_suppressions=open_suppressions,
    )


def qualified_slices(scan: CohortScanInput) -> list[CohortSlice]:
    qualified: list[CohortSlice] = []
    for item in scan.slices:
        failure_rate = item.failures / item.attempts if item.attempts else 0
        bank_errors = sum(item.top_reasons.get(reason, 0) for reason in BANK_OR_GATEWAY_REASONS)
        bank_share = bank_errors / item.failures if item.failures else 0
        if (
            item.attempts >= 20
            and failure_rate >= 3 * item.baseline_rate
            and bank_share >= 0.80
        ):
            qualified.append(item)
    return qualified


def evidence_matches_scope(evidence: dict[str, Any], scope: dict[str, str]) -> bool:
    return all(str(evidence.get(key)) == str(value) for key, value in scope.items())


def _case_evidence(session: Session, case_id: str) -> dict[str, Any]:
    event = session.scalar(
        select(Event)
        .where(Event.case_id == case_id, Event.kind.in_(["DETECTED", "SIGNAL"]))
        .order_by(Event.seq.desc())
    )
    return dict(event.payload.get("evidence", {})) if event is not None else {}


def case_matches_open_suppression(
    session: Session,
    case: RecoveryCase,
    *,
    now: datetime,
) -> bool:
    evidence = _case_evidence(session, case.id)
    return any(
        evidence_matches_scope(evidence, dict(item.scope))
        for item in session.scalars(
            select(Suppression).where(
                Suppression.merchant_id == case.merchant_id,
                Suppression.expires_at > _aware(now),
            )
        )
    )


def open_circuit_breaker(
    session: Session,
    *,
    merchant_id: str,
    anomaly: CohortAnomaly,
    opened_at: datetime,
) -> tuple[Suppression, int, bool]:
    now = _aware(opened_at)
    existing = next(
        (
            item
            for item in session.scalars(
                select(Suppression).where(
                    Suppression.merchant_id == merchant_id,
                    Suppression.expires_at > now,
                )
            )
            if item.pattern == anomaly.pattern and dict(item.scope) == anomaly.scope
        ),
        None,
    )
    if existing is not None:
        return existing, 0, False
    suppression = Suppression(
        merchant_id=merchant_id,
        scope=anomaly.scope,
        pattern=anomaly.pattern,
        reason=anomaly.evidence,
        opened_at=now,
        expires_at=now + timedelta(minutes=anomaly.ttl_minutes),
        opened_by="tier2",
    )
    session.add(suppression)
    session.flush()
    affected = 0
    cases = list(
        session.scalars(
            select(RecoveryCase).where(
                RecoveryCase.merchant_id == merchant_id,
                RecoveryCase.state.not_in([CaseState.CLOSED.value, CaseState.SUPPRESSED.value]),
                RecoveryCase.outcome.is_(None),
            )
        )
    )
    for case in cases:
        if not evidence_matches_scope(_case_evidence(session, case.id), anomaly.scope):
            continue
        actions = list(
            session.scalars(
                select(Action).where(Action.case_id == case.id, Action.status == "pending")
            )
        )
        for action in actions:
            action.status = "cancelled"
        case.outcome = CaseOutcome.SUPPRESSED.value
        append_event(
            session,
            case,
            kind="SUPPRESSED",
            payload={
                "suppression_id": suppression.id,
                "pattern": anomaly.pattern,
                "scope": anomaly.scope,
                "evidence": anomaly.evidence,
                "confidence": anomaly.confidence,
                "expires_at": suppression.expires_at.isoformat(),
                "cancelled_action_ids": [action.id for action in actions],
            },
            actor="tier2_circuit_breaker",
        )
        affected += 1
    session.flush()
    return suppression, affected, True


@dataclass(frozen=True)
class CohortRunResult:
    qualified_slices: int
    anomalies: int
    suppressions_opened: int
    cases_suppressed: int
    degraded: bool
    llm_call_id: int | None


def run_cohort_scan(
    session: Session,
    *,
    merchant_id: str,
    window_from: datetime,
    window_to: datetime,
    client: StructuredLLMClient | None = None,
) -> CohortRunResult:
    scan = aggregate_cohort_window(
        session,
        merchant_id=merchant_id,
        window_from=window_from,
        window_to=window_to,
    )
    candidates = qualified_slices(scan)
    if not candidates:
        return CohortRunResult(0, 0, 0, 0, False, None)
    candidate_scan = scan.model_copy(update={"slices": candidates})
    try:
        output, record = (client or StructuredLLMClient()).call(
            session,
            purpose="cohort_scan",
            payload=candidate_scan.model_dump(mode="json", by_alias=True),
            schema=CohortScanOutput,
        )
    except StructuredOutputError:
        session.commit()
        return CohortRunResult(len(candidates), 0, 0, 0, True, None)

    opened = 0
    suppressed = 0
    qualified_scopes = [item.dim for item in candidates]
    for anomaly in output.anomalies:
        if anomaly.scope not in qualified_scopes:
            continue
        if anomaly.recommended_action != "GLOBAL_SUPPRESS" or anomaly.confidence < 0.80:
            continue
        _, affected, created = open_circuit_breaker(
            session,
            merchant_id=merchant_id,
            anomaly=anomaly,
            opened_at=_aware(window_to),
        )
        opened += int(created)
        suppressed += affected
    session.commit()
    return CohortRunResult(
        len(candidates), len(output.anomalies), opened, suppressed, False, record.id
    )
