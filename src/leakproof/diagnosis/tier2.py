from __future__ import annotations

import hashlib
import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leakproof.audit.timeline import append_event
from leakproof.config import ModelTarget, get_policy_config
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
from leakproof.providers.contracts import (
    CohortAnalysisProvider,
    CohortAnalysisRequest,
    CohortAnalysisResult,
    ProviderError,
)

PROMPT_VERSION = "cohort_scan_v1"
MAX_QUALIFIED_SLICES = 20
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
    observed_failure_rate: float = Field(default=0, ge=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def derive_observed_rate(cls, value: Any) -> Any:
        if isinstance(value, dict) and "observed_failure_rate" not in value:
            attempts = int(value.get("attempts", 0))
            failures = int(value.get("failures", 0))
            value = {
                **value,
                "observed_failure_rate": failures / attempts if attempts else 0,
            }
        return value


class CohortBaseline(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attempts_7d: int = Field(default=0, ge=0)
    failures_7d: int = Field(default=0, ge=0)
    failure_rate_7d: float = Field(ge=0, le=1)
    failure_rate_1h: float = Field(ge=0, le=1)


class CohortSlice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    slice_id: str = ""
    dim: dict[str, str]
    attempts: int = Field(ge=0)
    failures: int = Field(ge=0)
    baseline_attempts: int = Field(default=100, ge=0)
    baseline_failures: int = Field(default=0, ge=0)
    baseline_rate: float = Field(ge=0, le=1)
    observed_failure_rate: float = Field(default=0, ge=0, le=1)
    failure_rate_change: float = Field(default=0, ge=-1, le=1)
    failure_rate_ratio: float | None = Field(default=None, ge=0)
    top_reasons: dict[str, int]

    @model_validator(mode="before")
    @classmethod
    def derive_observed_metrics_and_stable_id(cls, value: Any) -> Any:
        if isinstance(value, dict):
            value = dict(value)
            attempts = int(value.get("attempts", 0))
            failures = int(value.get("failures", 0))
            baseline_rate = float(value.get("baseline_rate", 0))
            observed_rate = failures / attempts if attempts else 0
            value.setdefault("observed_failure_rate", observed_rate)
            value.setdefault("failure_rate_change", observed_rate - baseline_rate)
            value.setdefault(
                "failure_rate_ratio",
                observed_rate / baseline_rate if baseline_rate else None,
            )
        if isinstance(value, dict) and not value.get("slice_id"):
            dim = value.get("dim") or {}
            digest = hashlib.sha256(
                json.dumps(dim, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()[:16]
            value = {**value, "slice_id": f"slice_{digest}"}
        return value

    @field_validator("dim")
    @classmethod
    def aggregate_dimensions_only(cls, value: dict[str, str]) -> dict[str, str]:
        allowed = {
            "issuer",
            "method",
            "bin",
            "checkout_step",
            "checkout_version",
            "payer_group",
        }
        if not value or not set(value).issubset(allowed):
            raise ValueError("cohort dimensions must be non-PII aggregate fields")
        return value


class CohortProposalConstraints(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_actions: list[str] = Field(
        default_factory=lambda: [
            "GLOBAL_SUPPRESS",
            "DELAY_RETRIES",
            "ALERT_MERCHANT",
            "NO_ACTION",
        ]
    )
    minimum_confidence: float = Field(default=0.80, ge=0, le=1)
    maximum_ttl_minutes: int = Field(default=120, ge=1, le=24 * 60)
    scope_rule: Literal["EXACT_SUPPLIED_SLICE"] = "EXACT_SUPPLIED_SLICE"


class CohortScanInput(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    window: CohortWindow
    totals: CohortTotals
    baseline: CohortBaseline
    slices: list[CohortSlice]
    proposal_constraints: CohortProposalConstraints = Field(
        default_factory=CohortProposalConstraints
    )
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
    evidence_slice_ids: list[str] = Field(min_length=1, max_length=5)
    scope: dict[str, str]
    evidence: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)
    recommended_action: Literal["GLOBAL_SUPPRESS", "DELAY_RETRIES", "ALERT_MERCHANT", "NO_ACTION"]
    ttl_minutes: int = Field(ge=1, le=24 * 60)

    @field_validator("scope", mode="before")
    @classmethod
    def supported_scope(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            raise ValueError("cohort scope must be an object")
        cleaned = {str(key): str(item) for key, item in value.items() if item is not None}
        return CohortSlice(
            dim=cleaned,
            attempts=0,
            failures=0,
            baseline_rate=0,
            top_reasons={},
        ).dim


class CohortScanOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    anomalies: list[CohortAnomaly]


def strict_cohort_output_schema() -> dict[str, Any]:
    """Return the OpenAI strict subset: closed objects and every property required."""
    schema = CohortScanOutput.model_json_schema()
    anomaly = schema["$defs"]["CohortAnomaly"]
    scope = anomaly["properties"]["scope"]
    scope_keys = (
        "issuer",
        "method",
        "bin",
        "checkout_step",
        "checkout_version",
        "payer_group",
    )
    scope.clear()
    scope.update(
        {
            "type": "object",
            "properties": {key: {"type": ["string", "null"]} for key in scope_keys},
            "required": list(scope_keys),
            "additionalProperties": False,
        }
    )
    return schema


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
            if not (attempts >= 20 and failure_rate >= 3 * baseline and bank_share >= 0.80):
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
                    "evidence_slice_ids": [item["slice_id"]],
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


class DeterministicCohortProvider:
    """Provider adapter reserved for simulation, offline evaluation, and tests."""

    def __init__(self, transport: CohortModelTransport | None = None) -> None:
        self.transport = transport or DeterministicCohortTransport()

    def analyze_cohort(self, request: CohortAnalysisRequest) -> CohortAnalysisResult:
        raw = self.transport.complete(
            system_prompt=request.instructions,
            payload=request.aggregate_payload,
            model=request.model,
            max_tokens=request.max_output_tokens,
        )
        return CohortAnalysisResult(
            data=raw.data,
            request_id="deterministic_simulation",
            input_tokens=raw.input_tokens,
            output_tokens=raw.output_tokens,
            cost_paise=raw.cost_paise,
            latency_ms=raw.latency_ms,
        )


class StructuredOutputError(RuntimeError):
    pass


OutputModel = TypeVar("OutputModel", bound=BaseModel)


class StructuredLLMClient:
    def __init__(
        self, provider: CohortAnalysisProvider | CohortModelTransport | None = None
    ) -> None:
        if provider is None:
            self.provider: CohortAnalysisProvider = DeterministicCohortProvider()
        elif hasattr(provider, "analyze_cohort"):
            self.provider = provider  # type: ignore[assignment]
        else:
            self.provider = DeterministicCohortProvider(provider)  # type: ignore[arg-type]

    def call(
        self,
        session: Session,
        *,
        purpose: str,
        payload: dict[str, Any],
        schema: type[OutputModel],
        merchant_id: str | None = None,
        case_id: str | None = None,
        batch_run_id: str | None = None,
    ) -> tuple[OutputModel, LLMCall]:
        config = get_policy_config()
        route = config.models.routes[purpose]
        spent_query = select(func.coalesce(func.sum(LLMCall.cost_paise), 0))
        if batch_run_id is not None:
            spent_query = spent_query.where(LLMCall.batch_run_id == batch_run_id)
            spent = int(session.scalar(spent_query) or 0)
        else:
            # A live scheduled scan is one bounded run and makes one logical provider call.
            spent = 0
        if spent >= config.models.budgets.per_batch_paise:
            record = LLMCall(
                case_id=case_id,
                merchant_id=merchant_id,
                batch_run_id=batch_run_id,
                purpose=purpose,
                provider="budget_guard",
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
            "those numbers, quote the counts used in evidence, use only the supplied scope and "
            "proposal constraints, and "
            "return NO_ACTION when a deviation is within noise. Return the required JSON schema."
        )
        target: ModelTarget = route.primary
        raw: CohortAnalysisResult | None = None
        parsed: OutputModel | None = None
        error_class: str | None = None
        try:
            raw = self.provider.analyze_cohort(
                CohortAnalysisRequest(
                    aggregate_payload=payload,
                    output_schema=(
                        strict_cohort_output_schema()
                        if schema is CohortScanOutput
                        else schema.model_json_schema()
                    ),
                    model=target.model,
                    max_output_tokens=target.max_tokens or 1200,
                    instructions=system_prompt,
                )
            )
            if spent + raw.cost_paise > config.models.budgets.per_batch_paise:
                error_class = "run_budget_exceeded"
            else:
                parsed = schema.model_validate(raw.data)
        except ProviderError as exc:
            error_class = exc.error_class
            raw = CohortAnalysisResult(
                data={},
                request_id=exc.request_id,
                input_tokens=exc.input_tokens,
                output_tokens=exc.output_tokens,
                cost_paise=exc.cost_paise,
                latency_ms=exc.latency_ms,
                attempts=exc.attempts,
            )
        except ValidationError:
            error_class = "invalid_schema"

        record = LLMCall(
            case_id=case_id,
            merchant_id=merchant_id,
            batch_run_id=batch_run_id,
            purpose=purpose,
            provider=(
                "openai"
                if self.provider.__class__.__name__.startswith("OpenAI")
                else "deterministic_simulation"
            ),
            request_id=raw.request_id if raw is not None else None,
            error_class=error_class,
            model=target.model,
            prompt_version=PROMPT_VERSION,
            input_tokens=raw.input_tokens if raw is not None else 0,
            output_tokens=raw.output_tokens if raw is not None else 0,
            cost_paise=raw.cost_paise if raw is not None else 0,
            latency_ms=raw.latency_ms if raw is not None else 0,
            schema_ok=parsed is not None,
            retries=max(0, (raw.attempts if raw is not None else 1) - 1),
        )
        session.add(record)
        session.flush()
        if parsed is None:
            raise StructuredOutputError("model returned unavailable or invalid structured output")
        return parsed, record


def aggregate_cohort_window(
    session: Session,
    *,
    merchant_id: str,
    window_from: datetime,
    window_to: datetime,
    batch_run_id: str | None = None,
) -> CohortScanInput:
    """Aggregate observed attempts without placing customer or entity identifiers in the payload."""
    start, end = _aware(window_from), _aware(window_to)
    if end <= start:
        raise ValueError("cohort window end must be after its start")
    if batch_run_id is not None:
        namespace = batch_run_id
    else:
        merchant = session.get(Merchant, merchant_id)
        simulation_run_id = (merchant.policy or {}).get("simulation_run_id") if merchant else None
        namespace = str(simulation_run_id) if simulation_run_id else "live"
    current_rows = list(
        session.scalars(
            select(PaymentAttemptObservation)
            .where(
                PaymentAttemptObservation.merchant_id == merchant_id,
                PaymentAttemptObservation.namespace == namespace,
                PaymentAttemptObservation.observed_at >= start,
                PaymentAttemptObservation.observed_at < end,
            )
            .order_by(PaymentAttemptObservation.observed_at, PaymentAttemptObservation.id)
        )
    )
    baseline_rows = list(
        session.scalars(
            select(PaymentAttemptObservation)
            .where(
                PaymentAttemptObservation.merchant_id == merchant_id,
                PaymentAttemptObservation.namespace == namespace,
                PaymentAttemptObservation.observed_at >= start - timedelta(days=7),
                PaymentAttemptObservation.observed_at < start,
            )
            .order_by(PaymentAttemptObservation.observed_at, PaymentAttemptObservation.id)
        )
    )

    def dimensions(item: PaymentAttemptObservation) -> dict[str, str]:
        if item.issuer != "unknown":
            values = {"issuer": item.issuer, "method": item.method}
        elif item.bin_bucket != "unknown":
            values = {"bin": item.bin_bucket}
        elif item.checkout_step != "unknown" or item.checkout_version != "unknown":
            values = {
                "checkout_step": item.checkout_step,
                "checkout_version": item.checkout_version,
            }
        else:
            values = {"method": item.method}
        return {key: value for key, value in values.items() if value != "unknown"}

    grouped: dict[tuple[tuple[str, str], ...], list[PaymentAttemptObservation]] = defaultdict(list)
    for observation in current_rows:
        dims = dimensions(observation)
        if dims:
            grouped[tuple(sorted(dims.items()))].append(observation)

    slices: list[CohortSlice] = []
    for dimension_items, observations in sorted(grouped.items()):
        dim = dict(dimension_items)
        historical = [
            item
            for item in baseline_rows
            if all(dimensions(item).get(key) == value for key, value in dim.items())
        ]
        failures = sum(item.outcome == "failure" for item in observations)
        baseline_failures = sum(item.outcome == "failure" for item in historical)
        baseline_rate = baseline_failures / len(historical) if historical else 0
        reasons = Counter(
            item.error_reason
            for item in observations
            if item.outcome == "failure" and item.error_reason != "unknown"
        )
        digest = hashlib.sha256(
            json.dumps(
                {"dim": dim, "from": start.isoformat(), "to": end.isoformat()},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:16]
        slices.append(
            CohortSlice(
                slice_id=f"slice_{digest}",
                dim=dim,
                attempts=len(observations),
                failures=failures,
                baseline_attempts=len(historical),
                baseline_failures=baseline_failures,
                baseline_rate=baseline_rate,
                top_reasons=dict(reasons.most_common(5)),
            )
        )
    open_suppressions = [
        {"id": item.id, "scope": item.scope, "pattern": item.pattern, "expires_at": item.expires_at}
        for item in session.scalars(
            select(Suppression)
            .where(
                Suppression.merchant_id == merchant_id,
                Suppression.expires_at > end,
            )
            .order_by(Suppression.expires_at)
            .limit(20)
        )
    ]
    total_attempts = len(current_rows)
    total_failures = sum(item.outcome == "failure" for item in current_rows)
    historical_failures = sum(item.outcome == "failure" for item in baseline_rows)
    baseline = historical_failures / len(baseline_rows) if baseline_rows else 0
    return CohortScanInput(
        window={"from": start, "to": end},
        totals={"attempts": total_attempts, "failures": total_failures},
        baseline={
            "attempts_7d": len(baseline_rows),
            "failures_7d": historical_failures,
            "failure_rate_7d": baseline,
            "failure_rate_1h": total_failures / total_attempts if total_attempts else 0,
        },
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
            and item.baseline_attempts >= 50
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
            kind="SUPPRESSION_OPENED",
            payload={
                "suppression_id": suppression.id,
                "pattern": anomaly.pattern,
                "scope": anomaly.scope,
                "ttl_minutes": anomaly.ttl_minutes,
                "cancelled_action_ids": [action.id for action in actions],
            },
            actor="deterministic_cohort_policy",
        )
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
    status: Literal["COMPLETED", "INSUFFICIENT_DATA", "DEGRADED"] = "COMPLETED"
    proposals_rejected: int = 0


@dataclass(frozen=True)
class ProposalVerdict:
    accepted: bool
    reason: str
    evidence_slice: CohortSlice | None


def validate_cohort_proposal(
    anomaly: CohortAnomaly, candidates: list[CohortSlice]
) -> ProposalVerdict:
    by_id = {item.slice_id: item for item in candidates}
    if not anomaly.evidence_slice_ids:
        return ProposalVerdict(False, "missing_evidence_slice_id", None)
    unknown_ids = set(anomaly.evidence_slice_ids) - set(by_id)
    if unknown_ids:
        return ProposalVerdict(False, "unsupported_evidence_slice_id", None)
    referenced = [by_id[item] for item in anomaly.evidence_slice_ids]
    # The supplied aggregate is the narrowest scope supported by its observed rows. Dropping a
    # dimension would broaden the actuator; adding one would invent evidence. A proposal must
    # therefore match one referenced slice exactly.
    supported = next((item for item in referenced if anomaly.scope == item.dim), None)
    if supported is None:
        return ProposalVerdict(False, "scope_expands_beyond_supplied_evidence", referenced[0])
    if supported not in qualified_slices(
        CohortScanInput(
            window={
                "from": datetime(2000, 1, 1, tzinfo=UTC),
                "to": datetime(2000, 1, 2, tzinfo=UTC),
            },
            totals={"attempts": supported.attempts, "failures": supported.failures},
            baseline={
                "failure_rate_7d": supported.baseline_rate,
                "failure_rate_1h": supported.baseline_rate,
            },
            slices=[supported],
        )
    ):
        return ProposalVerdict(False, "deterministic_thresholds_not_met", supported)
    if anomaly.confidence < 0.80:
        return ProposalVerdict(False, "confidence_below_policy_minimum", supported)
    if anomaly.ttl_minutes > 120:
        return ProposalVerdict(False, "ttl_exceeds_policy_maximum", supported)
    expected_pattern = (
        "issuer_outage"
        if "issuer" in supported.dim
        else "bin_rule_change"
        if "bin" in supported.dim
        else "checkout_regression"
        if "checkout_step" in supported.dim or "checkout_version" in supported.dim
        else "method_degradation"
        if "method" in supported.dim
        else "payer_cluster"
    )
    if anomaly.pattern != expected_pattern:
        return ProposalVerdict(False, "pattern_not_supported_by_scope", supported)
    if anomaly.recommended_action == "GLOBAL_SUPPRESS" and not (
        set(anomaly.scope) & {"issuer", "method", "bin"}
    ):
        return ProposalVerdict(False, "suppression_scope_not_supported", supported)
    return ProposalVerdict(True, "proposal_within_deterministic_policy", supported)


def _audit_cases(
    session: Session,
    *,
    merchant_id: str,
    evidence_slice: CohortSlice | None,
    window_from: datetime,
    window_to: datetime,
) -> list[RecoveryCase]:
    rows = list(
        session.scalars(
            select(RecoveryCase)
            .where(
                RecoveryCase.merchant_id == merchant_id,
                RecoveryCase.detected_at >= _aware(window_from),
                RecoveryCase.detected_at < _aware(window_to),
            )
            .order_by(RecoveryCase.detected_at, RecoveryCase.id)
        )
    )
    if evidence_slice is None:
        return rows[:1]
    matches = [
        case
        for case in rows
        if evidence_matches_scope(_case_evidence(session, case.id), evidence_slice.dim)
    ]
    return matches or rows[:1]


def _append_cohort_event(
    session: Session,
    cases: list[RecoveryCase],
    *,
    kind: str,
    payload: dict[str, Any],
) -> None:
    for case in cases:
        append_event(
            session,
            case,
            kind=kind,
            payload=payload,
            actor=(
                "openai_cohort_provider" if kind == "AI_PROPOSED" else "deterministic_cohort_policy"
            ),
        )


def run_cohort_scan(
    session: Session,
    *,
    merchant_id: str,
    window_from: datetime,
    window_to: datetime,
    client: StructuredLLMClient | None = None,
    provider: CohortAnalysisProvider | None = None,
    batch_run_id: str | None = None,
) -> CohortRunResult:
    scan = aggregate_cohort_window(
        session,
        merchant_id=merchant_id,
        window_from=window_from,
        window_to=window_to,
        batch_run_id=batch_run_id,
    )
    candidates = qualified_slices(scan)[:MAX_QUALIFIED_SLICES]
    if not candidates:
        return CohortRunResult(0, 0, 0, 0, False, None, status="INSUFFICIENT_DATA")
    candidate_scan = scan.model_copy(update={"slices": candidates})
    if client is None:
        if provider is None:
            from leakproof.providers.factory import get_cohort_analysis_provider

            provider = get_cohort_analysis_provider()
        client = StructuredLLMClient(provider)
    try:
        output, record = client.call(
            session,
            purpose="cohort_scan",
            merchant_id=merchant_id,
            payload=candidate_scan.model_dump(mode="json", by_alias=True),
            schema=CohortScanOutput,
            batch_run_id=batch_run_id,
        )
    except StructuredOutputError:
        anchors = _audit_cases(
            session,
            merchant_id=merchant_id,
            evidence_slice=candidates[0],
            window_from=window_from,
            window_to=window_to,
        )
        _append_cohort_event(
            session,
            anchors,
            kind="AI_DEGRADED",
            payload={"reason": "provider_or_schema_failure", "consequence": "NO_ACTION"},
        )
        _append_cohort_event(
            session,
            anchors,
            kind="NO_ACTION",
            payload={"reason": "AI proposal unavailable; deterministic recovery continues"},
        )
        session.commit()
        failed_query = select(LLMCall).where(
            LLMCall.purpose == "cohort_scan",
            LLMCall.merchant_id == merchant_id,
        )
        if batch_run_id is not None:
            failed_query = failed_query.where(LLMCall.batch_run_id == batch_run_id)
        failed_record = session.scalar(failed_query.order_by(LLMCall.id.desc()))
        return CohortRunResult(
            len(candidates),
            0,
            0,
            0,
            True,
            failed_record.id if failed_record else None,
            status="DEGRADED",
        )

    opened = 0
    suppressed = 0
    rejected = 0
    if not output.anomalies:
        anchors = _audit_cases(
            session,
            merchant_id=merchant_id,
            evidence_slice=candidates[0],
            window_from=window_from,
            window_to=window_to,
        )
        _append_cohort_event(
            session,
            anchors,
            kind="AI_PROPOSED",
            payload={
                "evidence_slice_ids": [item.slice_id for item in candidates],
                "recommended_action": "NO_ACTION",
                "evidence": "Model found no supported intervention.",
            },
        )
        _append_cohort_event(
            session,
            anchors,
            kind="POLICY_VALIDATED",
            payload={"recommended_action": "NO_ACTION", "reason": "no_supported_anomaly"},
        )
        _append_cohort_event(
            session,
            anchors,
            kind="NO_ACTION",
            payload={"reason": "no_supported_anomaly"},
        )
    for anomaly in output.anomalies:
        verdict = validate_cohort_proposal(anomaly, candidates)
        anchors = _audit_cases(
            session,
            merchant_id=merchant_id,
            evidence_slice=verdict.evidence_slice,
            window_from=window_from,
            window_to=window_to,
        )
        proposal_payload = anomaly.model_dump(mode="json")
        _append_cohort_event(session, anchors, kind="AI_PROPOSED", payload=proposal_payload)
        if not verdict.accepted:
            rejected += 1
            _append_cohort_event(
                session,
                anchors,
                kind="AI_PROPOSAL_REJECTED",
                payload={**proposal_payload, "reason": verdict.reason},
            )
            _append_cohort_event(
                session,
                anchors,
                kind="NO_ACTION",
                payload={"reason": verdict.reason},
            )
            continue
        _append_cohort_event(
            session,
            anchors,
            kind="POLICY_VALIDATED",
            payload={**proposal_payload, "reason": verdict.reason},
        )
        if anomaly.recommended_action == "NO_ACTION":
            _append_cohort_event(
                session, anchors, kind="NO_ACTION", payload={"reason": anomaly.evidence}
            )
            continue
        if anomaly.recommended_action == "ALERT_MERCHANT":
            _append_cohort_event(
                session,
                anchors,
                kind="MERCHANT_ALERTED",
                payload={"scope": anomaly.scope, "evidence": anomaly.evidence},
            )
            continue
        if anomaly.recommended_action == "DELAY_RETRIES":
            delayed = 0
            for case in anchors:
                for action in session.scalars(
                    select(Action).where(
                        Action.case_id == case.id,
                        Action.status == "pending",
                        Action.action_type == "silent_retry",
                    )
                ):
                    action.scheduled_for = action.scheduled_for + timedelta(
                        minutes=anomaly.ttl_minutes
                    )
                    delayed += 1
            _append_cohort_event(
                session,
                anchors,
                kind="RETRY_DELAYED",
                payload={
                    "scope": anomaly.scope,
                    "ttl_minutes": anomaly.ttl_minutes,
                    "actions_delayed": delayed,
                },
            )
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
        len(candidates),
        len(output.anomalies),
        opened,
        suppressed,
        False,
        record.id,
        status="COMPLETED",
        proposals_rejected=rejected,
    )
