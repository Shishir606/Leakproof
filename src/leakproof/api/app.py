from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from leakproof.audit.timeline import replay_case
from leakproof.celery_app import process_webhook
from leakproof.config import get_policy_config, get_settings
from leakproof.db import get_session
from leakproof.measurement import Scoreboard, compute_scoreboard
from leakproof.models.db import LLMCall, Suppression
from leakproof.models.domain import ReplayedCase
from leakproof.sensors.webhooks import (
    InvalidWebhookSignature,
    persist_webhook,
    verify_razorpay_signature,
)

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


@app.get("/cases/{case_id}/replay", response_model=ReplayedCase)
def case_replay(case_id: str, session: SessionDep) -> ReplayedCase:
    try:
        return replay_case(session, case_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="case not found") from exc


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


@app.get("/scoreboard/{run_id}", response_model=Scoreboard)
def scoreboard(run_id: str, session: SessionDep) -> Scoreboard:
    try:
        return compute_scoreboard(session, run_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="batch run not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
