from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from leakproof.api.access_logging import install_access_log_redaction
from leakproof.api.auth import OperatorPrincipal, get_operator_principal
from leakproof.audit.timeline import replay_case
from leakproof.batch import BatchResult, run_full_batch
from leakproof.celery_app import check_demo_abandonment, process_webhook
from leakproof.config import get_policy_config, get_settings
from leakproof.db import get_session
from leakproof.demo import (
    APIError,
    APIErrorDetail,
    CheckoutEventReceipt,
    CheckoutEventRequest,
    CheckoutPaymentVerificationReceipt,
    CheckoutPaymentVerificationRequest,
    DemoAcceptanceExport,
    DemoSessionCreated,
    DemoSessionCreateRequest,
    DemoSessionProjection,
    RazorpayWebhookEnvelope,
    RecoveryBootstrap,
    ResendWebhookEnvelope,
)
from leakproof.demo.acceptance import build_demo_acceptance_export
from leakproof.demo.projection import get_demo_session_projection
from leakproof.demo.rate_limit import RateLimitUnavailable
from leakproof.demo.service import (
    CheckoutPaymentNotCaptured,
    CheckoutPaymentProofInvalid,
    CheckoutPaymentVerificationUnavailable,
    DemoRateLimitExceeded,
    DemoSessionExpired,
    DemoSessionUnauthorized,
    RecoveryExpired,
    RecoveryOrderNotAvailable,
    RecoveryTokenInvalid,
    create_demo_session,
    get_recovery_bootstrap,
    ingest_checkout_event,
    verify_checkout_payment,
)
from leakproof.measurement import ExceptionReport, Scoreboard, compute_scoreboard, exception_report
from leakproof.models.db import (
    Action,
    BatchRun,
    Diagnosis,
    EvalRun,
    Event,
    LLMCall,
    Promise,
    RecoveryAttribution,
    RecoveryCase,
    Suppression,
)
from leakproof.models.domain import CaseState, LeakType, ReplayedCase
from leakproof.provenance import DataProvenance
from leakproof.providers import PaymentProvider, ProviderError
from leakproof.providers.factory import get_demo_rate_limiter, get_payment_provider
from leakproof.sensors.webhooks import (
    InvalidWebhookSignature,
    persist_webhook,
    verify_razorpay_signature,
    verify_resend_signature,
)
from leakproof.simulator.generate import generate_dataset, load_parameters
from leakproof.voice import handle_voice_turn

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    install_access_log_redaction()
    get_settings()  # enforce mode-specific startup gates
    get_policy_config()  # fail startup on malformed policy/configuration
    yield


app = FastAPI(title="Leakproof API", version="0.1.0", lifespan=lifespan)
SessionDep = Annotated[Session, Depends(get_session)]
PaymentProviderDep = Annotated[PaymentProvider, Depends(get_payment_provider)]
DemoRateLimiterDep = Annotated[Any, Depends(get_demo_rate_limiter)]
OperatorDep = Annotated[OperatorPrincipal, Depends(get_operator_principal)]


class WebhookAccepted(BaseModel):
    accepted: bool = True
    duplicate: bool
    webhook_id: int


class CapabilityView(BaseModel):
    capability: str
    data_provenance: DataProvenance
    scope: list[str]


class CapabilityContract(BaseModel):
    headline: str
    capabilities: list[CapabilityView]


def contract_error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = APIError(error=APIErrorDetail(code=code, message=message, retryable=retryable))
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers=headers,
    )


class SuppressionView(BaseModel):
    id: int
    merchant_id: str
    scope: dict[str, str]
    pattern: str
    reason: str
    opened_at: datetime
    expires_at: datetime
    opened_by: str


class EvalRunView(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    suite: str
    prompt_version: str | None
    model: str | None
    metrics: dict
    passed: bool
    ran_at: datetime


class LatestEvalsView(BaseModel):
    overall_passed: bool
    runs: list[EvalRunView]


class BatchRunRequest(BaseModel):
    seed: int = 42


class BatchRunView(BaseModel):
    run: dict[str, str | int | bool]
    scoreboard: Scoreboard
    exceptions: ExceptionReport


class CaseListItem(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    batch_run_id: str | None
    customer_id: str
    leak_type: str
    entity_type: str
    entity_id: str
    amount_at_risk: int
    currency: str
    state: str
    arm: str
    outcome: str | None
    detected_at: datetime
    closed_at: datetime | None
    event_count: int = 0


class CaseListView(BaseModel):
    items: list[CaseListItem]
    total: int
    limit: int
    offset: int


class DiagnosisView(BaseModel):
    model_config = {"from_attributes": True}

    tier: int
    failure_class: str
    confidence: float
    evidence: dict
    rule_id: str | None
    diagnosed_at: datetime


class ActionView(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    step_index: int
    action_type: str
    scheduled_for: datetime
    verdict: str | None
    verdict_rules: dict | None
    executed_at: datetime | None
    provider_ref: str | None
    status: str | None
    attempt_count: int
    cost_paise: int
    ev_estimate: int | None


class AttributionView(BaseModel):
    model_config = {"from_attributes": True}

    amount_paise: int
    matched_by: str
    credit_rule: str
    credited_action_type: str | None
    organic: bool
    paid_at: datetime


class PromiseView(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    case_id: str
    promised_on: date
    amount_paise: int
    captured_via: str
    kept: bool | None
    transcript_ref: str | None


class VoiceTurnRequest(BaseModel):
    provider_turn_id: str
    transcript: str
    occurred_at: datetime | None = None


class VoiceTurnView(BaseModel):
    action_id: str
    case_id: str
    provider_turn_id: str
    turn_number: int
    intent: str
    reply_template_id: str
    reply: str
    ended: bool
    promise_id: int | None
    replayed: bool


class CaseDetailView(BaseModel):
    case: CaseListItem
    replay: ReplayedCase
    diagnosis: DiagnosisView | None
    actions: list[ActionView]
    attribution: AttributionView | None
    promises: list[PromiseView]


def enqueue_webhook(webhook_id: int) -> None:
    try:
        process_webhook.delay(webhook_id)
    except Exception:
        # The committed inbox row remains recoverable by the Beat dispatcher.
        logger.exception("webhook persisted; immediate enqueue failed", extra={"id": webhook_id})


def _scoped_case(session: Session, case_id: str, principal: OperatorPrincipal) -> RecoveryCase:
    filters = [RecoveryCase.id == case_id]
    if not principal.all_merchants:
        filters.append(RecoveryCase.merchant_id.in_(principal.merchant_ids))
    case = session.scalar(select(RecoveryCase).where(*filters))
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    return case


def _scoped_batch(session: Session, run_id: str, principal: OperatorPrincipal) -> BatchRun:
    filters = [BatchRun.id == run_id]
    if not principal.all_merchants:
        filters.append(BatchRun.merchant_id.in_(principal.merchant_ids))
    run = session.scalar(select(BatchRun).where(*filters))
    if run is None:
        raise HTTPException(status_code=404, detail="batch run not found")
    return run


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def ready(session: SessionDep) -> dict[str, str]:
    try:
        session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ready"}


@app.get("/capabilities", response_model=CapabilityContract)
def capabilities() -> CapabilityContract:
    return CapabilityContract(
        headline="one live recovery loop; five simulated expansion surfaces",
        capabilities=[
            CapabilityView(
                capability="Razorpay recovery loop",
                data_provenance=DataProvenance.LIVE_PROVIDER_VERIFIED,
                scope=["payment failure", "checkout abandonment"],
            ),
            CapabilityView(
                capability="Scenario Lab",
                data_provenance=DataProvenance.SIMULATED_END_TO_END,
                scope=[
                    "payment failure",
                    "checkout abandonment",
                    "invoice overdue",
                    "subscription halt",
                    "mandate broken",
                ],
            ),
            CapabilityView(
                capability="voice/promise provider integration",
                data_provenance=DataProvenance.ARCHITECTURE_READY,
                scope=["bounded dialogue and promise capture"],
            ),
        ],
    )


@app.post(
    "/webhooks/razorpay",
    response_model=WebhookAccepted,
    status_code=status.HTTP_200_OK,
)
async def razorpay_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: SessionDep,
    x_razorpay_signature: str = Header(default=""),
    x_razorpay_event_id: str | None = Header(default=None),
    x_leakproof_merchant_id: str | None = Header(default=None),
) -> WebhookAccepted | JSONResponse:
    body = await request.body()
    settings = get_settings()
    if not settings.razorpay_webhook_secret:
        return contract_error(
            503,
            "integration_not_ready",
            "Razorpay webhook verification is not configured",
            retryable=True,
        )
    try:
        verify_razorpay_signature(body, x_razorpay_signature, settings.razorpay_webhook_secret)
    except InvalidWebhookSignature as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    try:
        envelope = RazorpayWebhookEnvelope.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid Razorpay webhook envelope") from exc
    payload = envelope.model_dump(mode="json")

    ingested = persist_webhook(
        session,
        merchant_id=x_leakproof_merchant_id or settings.default_merchant_id,
        payload=payload,
        header_event_id=x_razorpay_event_id,
    )
    if not ingested.duplicate:
        # Queue I/O happens after the response is sent; Beat rescues a missed handoff.
        background_tasks.add_task(enqueue_webhook, ingested.id)
    return WebhookAccepted(duplicate=ingested.duplicate, webhook_id=ingested.id)


@app.post(
    "/webhooks/resend",
    response_model=WebhookAccepted,
    responses={503: {"model": APIError}},
)
async def resend_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: SessionDep,
    svix_id: str = Header(default=""),
    svix_timestamp: str = Header(default=""),
    svix_signature: str = Header(default=""),
) -> WebhookAccepted | JSONResponse:
    settings = get_settings()
    if not settings.resend_webhook_secret:
        return contract_error(
            503,
            "integration_not_ready",
            "Resend webhook verification is not configured",
            retryable=True,
        )
    body = await request.body()
    try:
        verify_resend_signature(
            body,
            message_id=svix_id,
            timestamp=svix_timestamp,
            signature=svix_signature,
            secret=settings.resend_webhook_secret,
        )
    except InvalidWebhookSignature as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    try:
        envelope = ResendWebhookEnvelope.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid Resend webhook envelope") from exc
    # Delivery webhooks can contain recipient fields. Persist only the event material needed
    # for reconciliation so addresses cannot escape through logs, APIs, or audit projections.
    payload = {
        "type": envelope.type,
        "created_at": envelope.created_at.isoformat(),
        "data": {"email_id": envelope.data.email_id},
    }
    ingested = persist_webhook(
        session,
        merchant_id=settings.default_merchant_id,
        payload=payload,
        header_event_id=svix_id,
        provider="resend",
    )
    if not ingested.duplicate:
        background_tasks.add_task(enqueue_webhook, ingested.id)
    return WebhookAccepted(duplicate=ingested.duplicate, webhook_id=ingested.id)


@app.post(
    "/demo/sessions",
    response_model=DemoSessionCreated,
    status_code=status.HTTP_201_CREATED,
    responses={429: {"model": APIError}, 502: {"model": APIError}, 503: {"model": APIError}},
)
def create_demo_session_route(
    payload: DemoSessionCreateRequest,
    request: Request,
    session: SessionDep,
    provider: PaymentProviderDep,
    limiter: DemoRateLimiterDep,
) -> DemoSessionCreated | JSONResponse:
    settings = get_settings()
    if not settings.demo_sessions_enabled:
        return contract_error(
            503,
            "demo_sessions_disabled",
            "new demo sessions are temporarily disabled",
            retryable=True,
        )
    client_ip = request.client.host if request.client is not None else "unknown"
    try:
        return create_demo_session(
            session,
            payload,
            client_ip=client_ip,
            provider=provider,
            limiter=limiter,
            settings=settings,
        )
    except DemoRateLimitExceeded as exc:
        return contract_error(
            429,
            "rate_limit_exceeded",
            "demo session limit exceeded",
            retryable=True,
            headers={"Retry-After": str(max(1, exc.retry_after_seconds))},
        )
    except RateLimitUnavailable:
        return contract_error(
            503,
            "rate_limit_unavailable",
            "demo session creation is temporarily unavailable",
            retryable=True,
        )
    except ProviderError as exc:
        return contract_error(
            503 if exc.retryable else 502,
            exc.error_class,
            "Razorpay order creation failed",
            retryable=exc.retryable,
        )


@app.post(
    "/demo/sessions/{session_id}/checkout-events",
    response_model=CheckoutEventReceipt,
    responses={
        401: {"model": APIError},
        410: {"model": APIError},
        429: {"model": APIError},
        503: {"model": APIError},
    },
)
def checkout_event_route(
    session_id: str,
    payload: CheckoutEventRequest,
    session: SessionDep,
    limiter: DemoRateLimiterDep,
    x_leakproof_session_token: str = Header(default=""),
) -> CheckoutEventReceipt | JSONResponse:
    if not x_leakproof_session_token:
        return contract_error(401, "session_token_required", "session token is required")
    settings = get_settings()
    try:
        ingested = ingest_checkout_event(
            session,
            session_id,
            payload,
            session_token=x_leakproof_session_token,
            limiter=limiter,
            settings=settings,
        )
    except DemoSessionUnauthorized:
        return contract_error(401, "invalid_session_token", "invalid session token")
    except DemoSessionExpired:
        return contract_error(410, "session_expired", "demo session has expired")
    except DemoRateLimitExceeded as exc:
        return contract_error(
            429,
            "rate_limit_exceeded",
            "checkout event limit exceeded",
            retryable=exc.retry_after_seconds > 0,
            headers=(
                {"Retry-After": str(max(1, exc.retry_after_seconds))}
                if exc.retry_after_seconds > 0
                else None
            ),
        )
    except RateLimitUnavailable:
        return contract_error(
            503,
            "rate_limit_unavailable",
            "checkout telemetry is temporarily unavailable",
            retryable=True,
        )

    if ingested.dismissal_event_id is not None:
        try:
            check_demo_abandonment.apply_async(
                args=[session_id, ingested.dismissal_event_id],
                countdown=settings.demo_abandonment_delay_seconds,
            )
        except Exception:
            logger.exception(
                "dismissal persisted; immediate abandonment enqueue failed",
                extra={"session_id": session_id, "event_id": ingested.dismissal_event_id},
            )
    return ingested.receipt


@app.post(
    "/demo/sessions/{session_id}/payments/verify",
    response_model=CheckoutPaymentVerificationReceipt,
    responses={
        401: {"model": APIError},
        409: {"model": APIError},
        410: {"model": APIError},
        429: {"model": APIError},
        502: {"model": APIError},
        503: {"model": APIError},
    },
)
def checkout_payment_verification_route(
    session_id: str,
    payload: CheckoutPaymentVerificationRequest,
    session: SessionDep,
    provider: PaymentProviderDep,
    limiter: DemoRateLimiterDep,
    x_leakproof_session_token: str = Header(default=""),
    x_leakproof_recovery_token: str = Header(default=""),
) -> CheckoutPaymentVerificationReceipt | JSONResponse:
    if not x_leakproof_session_token and not x_leakproof_recovery_token:
        return contract_error(
            401,
            "payment_verification_token_required",
            "a session or recovery token is required",
        )
    try:
        return verify_checkout_payment(
            session,
            session_id,
            payload,
            provider=provider,
            limiter=limiter,
            settings=get_settings(),
            session_token=x_leakproof_session_token,
            recovery_token=x_leakproof_recovery_token,
        )
    except DemoSessionUnauthorized:
        return contract_error(
            401,
            "invalid_payment_verification_token",
            "payment verification authorization is invalid",
        )
    except CheckoutPaymentProofInvalid:
        return contract_error(
            401,
            "invalid_checkout_payment_proof",
            "Razorpay payment proof is invalid",
        )
    except DemoSessionExpired:
        return contract_error(410, "session_expired", "demo session has expired")
    except CheckoutPaymentNotCaptured:
        return contract_error(
            409,
            "payment_not_captured",
            "Razorpay has not captured the payment yet; retry verification shortly",
            retryable=True,
        )
    except DemoRateLimitExceeded as exc:
        return contract_error(
            429,
            "rate_limit_exceeded",
            "payment verification limit exceeded",
            retryable=True,
            headers={"Retry-After": str(max(1, exc.retry_after_seconds))},
        )
    except RateLimitUnavailable:
        return contract_error(
            503,
            "rate_limit_unavailable",
            "payment verification is temporarily unavailable",
            retryable=True,
        )
    except CheckoutPaymentVerificationUnavailable:
        return contract_error(
            503,
            "test_payment_verification_unavailable",
            "Razorpay test-mode payment verification is not configured",
            retryable=True,
        )
    except ProviderError as exc:
        return contract_error(
            503 if exc.retryable else 502,
            exc.error_class,
            "Razorpay payment-state verification failed",
            retryable=exc.retryable,
        )


@app.get(
    "/recover/{signed_token}",
    response_model=RecoveryBootstrap,
    responses={
        404: {"model": APIError},
        409: {"model": APIError},
        410: {"model": APIError},
        502: {"model": APIError},
        503: {"model": APIError},
    },
)
def recovery_route(
    signed_token: str,
    session: SessionDep,
    provider: PaymentProviderDep,
) -> RecoveryBootstrap | JSONResponse:
    try:
        return get_recovery_bootstrap(
            session,
            signed_token,
            provider=provider,
            settings=get_settings(),
        )
    except RecoveryTokenInvalid:
        # Do not reveal which bound claim failed.
        return contract_error(404, "invalid_recovery_token", "recovery link is invalid")
    except RecoveryExpired:
        return contract_error(410, "recovery_expired", "recovery link has expired")
    except RecoveryOrderNotAvailable:
        return contract_error(
            409,
            "order_not_recoverable",
            "the original order is no longer available for recovery",
        )
    except ProviderError as exc:
        return contract_error(
            503 if exc.retryable else 502,
            exc.error_class,
            "Razorpay payment-state verification failed",
            retryable=exc.retryable,
        )


@app.get(
    "/demo/sessions/{session_id}",
    response_model=DemoSessionProjection,
    responses={401: {"model": APIError}, 410: {"model": APIError}},
)
def demo_session_projection(
    session_id: str,
    session: SessionDep,
    x_leakproof_session_token: str = Header(default=""),
) -> DemoSessionProjection | JSONResponse:
    if not x_leakproof_session_token:
        return contract_error(401, "session_token_required", "session token is required")
    try:
        return get_demo_session_projection(
            session,
            session_id,
            session_token=x_leakproof_session_token,
            settings=get_settings(),
        )
    except DemoSessionUnauthorized:
        return contract_error(401, "invalid_session_token", "invalid session token")
    except DemoSessionExpired:
        return contract_error(410, "session_expired", "demo session has expired")


@app.get(
    "/demo/sessions/{session_id}/acceptance.json",
    response_model=DemoAcceptanceExport,
    responses={401: {"model": APIError}, 410: {"model": APIError}},
)
def demo_session_acceptance_export(
    session_id: str,
    session: SessionDep,
    x_leakproof_session_token: str = Header(default=""),
) -> DemoAcceptanceExport | JSONResponse:
    if not x_leakproof_session_token:
        return contract_error(401, "session_token_required", "session token is required")
    try:
        return build_demo_acceptance_export(
            session,
            session_id,
            session_token=x_leakproof_session_token,
            settings=get_settings(),
        )
    except DemoSessionUnauthorized:
        return contract_error(401, "invalid_session_token", "invalid session token")
    except DemoSessionExpired:
        return contract_error(410, "session_expired", "demo session has expired")


@app.get("/cases", response_model=CaseListView)
def cases(
    session: SessionDep,
    principal: OperatorDep,
    state_filter: Annotated[CaseState | None, Query(alias="state")] = None,
    leak_type: LeakType | None = None,
    batch_run_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CaseListView:
    filters = []
    if not principal.all_merchants:
        filters.append(RecoveryCase.merchant_id.in_(principal.merchant_ids))
    if state_filter:
        filters.append(RecoveryCase.state == state_filter.value)
    if leak_type:
        filters.append(RecoveryCase.leak_type == leak_type.value)
    if batch_run_id:
        filters.append(RecoveryCase.batch_run_id == batch_run_id)

    total = int(session.scalar(select(func.count(RecoveryCase.id)).where(*filters)) or 0)
    event_counts = (
        select(Event.case_id, func.count(Event.id).label("event_count"))
        .group_by(Event.case_id)
        .subquery()
    )
    rows = session.execute(
        select(RecoveryCase, func.coalesce(event_counts.c.event_count, 0))
        .outerjoin(event_counts, event_counts.c.case_id == RecoveryCase.id)
        .where(*filters)
        .order_by(RecoveryCase.detected_at.desc(), RecoveryCase.id)
        .offset(offset)
        .limit(limit)
    ).all()
    return CaseListView(
        items=[
            CaseListItem(
                **CaseListItem.model_validate(case, from_attributes=True).model_dump(
                    exclude={"event_count"}
                ),
                event_count=int(event_count),
            )
            for case, event_count in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@app.get("/cases/{case_id}/replay", response_model=ReplayedCase)
def case_replay(case_id: str, session: SessionDep, principal: OperatorDep) -> ReplayedCase:
    _scoped_case(session, case_id, principal)
    try:
        return replay_case(session, case_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="case not found") from exc


@app.get("/cases/{case_id}/audit.json", response_model=ReplayedCase)
def case_audit(case_id: str, session: SessionDep, principal: OperatorDep) -> ReplayedCase:
    _scoped_case(session, case_id, principal)
    try:
        return replay_case(session, case_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="case not found") from exc


@app.get("/cases/{case_id}", response_model=CaseDetailView)
def case_detail(case_id: str, session: SessionDep, principal: OperatorDep) -> CaseDetailView:
    case_row = _scoped_case(session, case_id, principal)
    try:
        replay = replay_case(session, case_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="case not found") from exc
    diagnosis = session.get(Diagnosis, case_id)
    actions = list(
        session.scalars(select(Action).where(Action.case_id == case_id).order_by(Action.step_index))
    )
    attribution = session.scalar(
        select(RecoveryAttribution).where(RecoveryAttribution.case_id == case_id)
    )
    promises = list(
        session.scalars(
            select(Promise)
            .where(Promise.case_id == case_id)
            .order_by(Promise.promised_on, Promise.id)
        )
    )
    return CaseDetailView(
        case=CaseListItem(
            **CaseListItem.model_validate(case_row, from_attributes=True).model_dump(
                exclude={"event_count"}
            ),
            event_count=len(replay.events),
        ),
        replay=replay,
        diagnosis=(
            DiagnosisView.model_validate(diagnosis, from_attributes=True)
            if diagnosis is not None
            else None
        ),
        actions=[ActionView.model_validate(action, from_attributes=True) for action in actions],
        attribution=(
            AttributionView.model_validate(attribution, from_attributes=True)
            if attribution is not None
            else None
        ),
        promises=[PromiseView.model_validate(item, from_attributes=True) for item in promises],
    )


@app.post("/actions/{action_id}/voice/turns", response_model=VoiceTurnView)
def voice_turn(
    action_id: str,
    request: VoiceTurnRequest,
    session: SessionDep,
    principal: OperatorDep,
) -> VoiceTurnView:
    action_case = session.execute(
        select(Action, RecoveryCase)
        .join(RecoveryCase, RecoveryCase.id == Action.case_id)
        .where(Action.id == action_id)
    ).one_or_none()
    if action_case is None or not principal.permits(action_case[1].merchant_id):
        raise HTTPException(status_code=404, detail="action not found")
    try:
        reply = handle_voice_turn(
            session,
            action_id,
            provider_turn_id=request.provider_turn_id,
            transcript=request.transcript,
            occurred_at=request.occurred_at,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="action not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return VoiceTurnView(
        action_id=reply.action_id,
        case_id=reply.case_id,
        provider_turn_id=reply.provider_turn_id,
        turn_number=reply.turn_number,
        intent=reply.intent,
        reply_template_id=reply.message.template_id,
        reply=reply.message.body,
        ended=reply.ended,
        promise_id=reply.promise_id,
        replayed=reply.replayed,
    )


@app.get("/suppressions", response_model=list[SuppressionView])
def open_suppressions(session: SessionDep, principal: OperatorDep) -> list[SuppressionView]:
    filters = [Suppression.expires_at > datetime.now(UTC)]
    if not principal.all_merchants:
        filters.append(Suppression.merchant_id.in_(principal.merchant_ids))
    rows = session.scalars(
        select(Suppression).where(*filters).order_by(Suppression.opened_at.desc())
    )
    return [SuppressionView.model_validate(item, from_attributes=True) for item in rows]


@app.post("/suppressions/{suppression_id}/close", response_model=SuppressionView)
def close_suppression(
    suppression_id: int, session: SessionDep, principal: OperatorDep
) -> SuppressionView:
    suppression = session.get(Suppression, suppression_id)
    if suppression is None or not principal.permits(suppression.merchant_id):
        raise HTTPException(status_code=404, detail="suppression not found")
    suppression.expires_at = datetime.now(UTC)
    session.commit()
    return SuppressionView.model_validate(suppression, from_attributes=True)


@app.get("/costs")
def llm_costs(session: SessionDep, principal: OperatorDep, run_id: str | None = None) -> dict:
    if run_id is not None:
        _scoped_batch(session, run_id, principal)
    run_filter = [LLMCall.batch_run_id == run_id] if run_id else []
    if not principal.all_merchants:
        scoped_cases = select(RecoveryCase.id).where(
            RecoveryCase.merchant_id.in_(principal.merchant_ids)
        )
        scoped_runs = select(BatchRun.id).where(BatchRun.merchant_id.in_(principal.merchant_ids))
        run_filter.append(
            (LLMCall.merchant_id.in_(principal.merchant_ids))
            | (LLMCall.case_id.in_(scoped_cases))
            | (LLMCall.batch_run_id.in_(scoped_runs))
        )
    totals = session.execute(
        select(
            func.count(LLMCall.id),
            func.coalesce(func.sum(LLMCall.input_tokens), 0),
            func.coalesce(func.sum(LLMCall.output_tokens), 0),
            func.coalesce(func.sum(LLMCall.cost_paise), 0),
            func.coalesce(func.sum(LLMCall.latency_ms), 0),
        ).where(*run_filter)
    ).one()
    schema_ok_calls = int(
        session.scalar(
            select(func.count(LLMCall.id)).where(LLMCall.schema_ok.is_(True), *run_filter)
        )
        or 0
    )
    by_purpose = session.execute(
        select(
            LLMCall.purpose,
            func.count(LLMCall.id),
            func.coalesce(func.sum(LLMCall.cost_paise), 0),
        )
        .where(*run_filter)
        .group_by(LLMCall.purpose)
    ).all()
    calls = int(totals[0])
    return {
        "calls": calls,
        "input_tokens": int(totals[1]),
        "output_tokens": int(totals[2]),
        "cost_paise": int(totals[3]),
        "latency_ms": int(totals[4]),
        "schema_ok_calls": schema_ok_calls,
        "schema_failed_calls": calls - schema_ok_calls,
        "by_purpose": [
            {"purpose": purpose, "calls": int(count), "cost_paise": int(cost)}
            for purpose, count, cost in by_purpose
        ],
    }


@app.get("/evals/latest", response_model=LatestEvalsView)
def latest_evals(session: SessionDep, _principal: OperatorDep) -> LatestEvalsView:
    latest_by_suite: dict[str, EvalRun] = {}
    for run in session.scalars(select(EvalRun).order_by(EvalRun.ran_at.desc(), EvalRun.id.desc())):
        latest_by_suite.setdefault(run.suite, run)
    if not latest_by_suite:
        raise HTTPException(status_code=404, detail="no evaluation runs found")
    views = [
        EvalRunView.model_validate(run, from_attributes=True)
        for run in sorted(latest_by_suite.values(), key=lambda item: item.suite)
    ]
    return LatestEvalsView(
        overall_passed=all(item.passed for item in views),
        runs=views,
    )


@app.post("/batch/run", response_model=BatchRunView)
def run_batch(
    request: BatchRunRequest, session: SessionDep, principal: OperatorDep
) -> BatchRunView:
    if get_settings().mode != "simulation":
        raise HTTPException(status_code=409, detail="batch simulation requires MODE=simulation")
    parameters = load_parameters()
    dataset = generate_dataset(parameters, seed=request.seed)
    if not principal.permits(dataset.merchant_id):
        raise HTTPException(status_code=404, detail="merchant not found")
    tuned_parameters = parameters.model_copy(
        update={"simulation": parameters.simulation.model_copy(update={"seed": request.seed})}
    )
    result: BatchResult = run_full_batch(session, dataset, tuned_parameters)
    return BatchRunView(
        run=result.as_dict(),
        scoreboard=compute_scoreboard(session, dataset.run_id),
        exceptions=exception_report(session, dataset.run_id),
    )


@app.get("/scoreboard/latest", response_model=Scoreboard)
def latest_scoreboard(session: SessionDep, principal: OperatorDep) -> Scoreboard:
    filters = []
    if not principal.all_merchants:
        filters.append(BatchRun.merchant_id.in_(principal.merchant_ids))
    run = session.scalar(
        select(BatchRun)
        .join(RecoveryCase, RecoveryCase.batch_run_id == BatchRun.id)
        .where(*filters)
        .order_by(BatchRun.started_at.desc(), BatchRun.id.desc())
        .limit(1)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="no batch runs found")
    try:
        return compute_scoreboard(session, run.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/scoreboard/{run_id}", response_model=Scoreboard)
def scoreboard(run_id: str, session: SessionDep, principal: OperatorDep) -> Scoreboard:
    _scoped_batch(session, run_id, principal)
    try:
        return compute_scoreboard(session, run_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="batch run not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/scoreboard/{run_id}/exceptions", response_model=ExceptionReport)
def scoreboard_exceptions(
    run_id: str, session: SessionDep, principal: OperatorDep
) -> ExceptionReport:
    _scoped_batch(session, run_id, principal)
    return exception_report(session, run_id)
