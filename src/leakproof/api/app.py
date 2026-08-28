from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from leakproof.audit.timeline import replay_case
from leakproof.celery_app import process_webhook
from leakproof.config import get_policy_config, get_settings
from leakproof.db import get_session
from leakproof.measurement import Scoreboard, compute_scoreboard
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
from leakproof.sensors.webhooks import (
    InvalidWebhookSignature,
    persist_webhook,
    verify_razorpay_signature,
)
from leakproof.voice import handle_voice_turn

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_policy_config()  # fail startup on malformed policy/configuration
    yield


app = FastAPI(title="Leakproof API", version="0.1.0", lifespan=lifespan)
SessionDep = Annotated[Session, Depends(get_session)]


class WebhookAccepted(BaseModel):
    accepted: bool = True
    duplicate: bool
    webhook_id: int


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
) -> WebhookAccepted:
    body = await request.body()
    settings = get_settings()
    try:
        verify_razorpay_signature(body, x_razorpay_signature, settings.razorpay_webhook_secret)
    except InvalidWebhookSignature as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON payload") from exc
    if not isinstance(payload, dict) or not payload.get("event"):
        raise HTTPException(status_code=422, detail="webhook event is required")

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


@app.get("/cases", response_model=CaseListView)
def cases(
    session: SessionDep,
    state_filter: Annotated[CaseState | None, Query(alias="state")] = None,
    leak_type: LeakType | None = None,
    batch_run_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CaseListView:
    filters = []
    if state_filter:
        filters.append(RecoveryCase.state == state_filter.value)
    if leak_type:
        filters.append(RecoveryCase.leak_type == leak_type.value)
    if batch_run_id:
        filters.append(RecoveryCase.batch_run_id == batch_run_id)

    total = int(
        session.scalar(select(func.count(RecoveryCase.id)).where(*filters)) or 0
    )
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
def case_replay(case_id: str, session: SessionDep) -> ReplayedCase:
    try:
        return replay_case(session, case_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="case not found") from exc


@app.get("/cases/{case_id}/audit.json", response_model=ReplayedCase)
def case_audit(case_id: str, session: SessionDep) -> ReplayedCase:
    try:
        return replay_case(session, case_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="case not found") from exc


@app.get("/cases/{case_id}", response_model=CaseDetailView)
def case_detail(case_id: str, session: SessionDep) -> CaseDetailView:
    try:
        replay = replay_case(session, case_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="case not found") from exc
    diagnosis = session.get(Diagnosis, case_id)
    actions = list(
        session.scalars(
            select(Action).where(Action.case_id == case_id).order_by(Action.step_index)
        )
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
            **CaseListItem.model_validate(
                session.get(RecoveryCase, case_id), from_attributes=True
            ).model_dump(exclude={"event_count"}),
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
) -> VoiceTurnView:
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
def open_suppressions(session: SessionDep) -> list[SuppressionView]:
    rows = session.scalars(
        select(Suppression)
        .where(Suppression.expires_at > datetime.now(UTC))
        .order_by(Suppression.opened_at.desc())
    )
    return [SuppressionView.model_validate(item, from_attributes=True) for item in rows]


@app.post("/suppressions/{suppression_id}/close", response_model=SuppressionView)
def close_suppression(suppression_id: int, session: SessionDep) -> SuppressionView:
    suppression = session.get(Suppression, suppression_id)
    if suppression is None:
        raise HTTPException(status_code=404, detail="suppression not found")
    suppression.expires_at = datetime.now(UTC)
    session.commit()
    return SuppressionView.model_validate(suppression, from_attributes=True)


@app.get("/costs")
def llm_costs(session: SessionDep) -> dict:
    totals = session.execute(
        select(
            func.count(LLMCall.id),
            func.coalesce(func.sum(LLMCall.input_tokens), 0),
            func.coalesce(func.sum(LLMCall.output_tokens), 0),
            func.coalesce(func.sum(LLMCall.cost_paise), 0),
            func.coalesce(func.sum(LLMCall.latency_ms), 0),
        )
    ).one()
    schema_ok_calls = int(
        session.scalar(
            select(func.count(LLMCall.id)).where(LLMCall.schema_ok.is_(True))
        )
        or 0
    )
    by_purpose = session.execute(
        select(
            LLMCall.purpose,
            func.count(LLMCall.id),
            func.coalesce(func.sum(LLMCall.cost_paise), 0),
        ).group_by(LLMCall.purpose)
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
def latest_evals(session: SessionDep) -> LatestEvalsView:
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


@app.get("/scoreboard/latest", response_model=Scoreboard)
def latest_scoreboard(session: SessionDep) -> Scoreboard:
    run = session.scalar(
        select(BatchRun)
        .join(RecoveryCase, RecoveryCase.batch_run_id == BatchRun.id)
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
def scoreboard(run_id: str, session: SessionDep) -> Scoreboard:
    try:
        return compute_scoreboard(session, run_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="batch run not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
