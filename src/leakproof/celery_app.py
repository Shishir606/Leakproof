from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from celery import Celery
from celery.schedules import crontab
from sqlalchemy import select

from leakproof.actuators import due_action_ids, execute_action
from leakproof.config import get_settings
from leakproof.db import SessionLocal
from leakproof.demo.email import execute_demo_recovery_email, schedule_demo_recovery_email
from leakproof.demo.insights import generate_case_insight, mark_case_insight_pending
from leakproof.demo.service import due_abandonment_checks, materialize_checkout_abandonment
from leakproof.diagnosis import diagnose_case
from leakproof.diagnosis.tier2 import run_cohort_scan
from leakproof.models.db import (
    Action,
    CaseInsightRecord,
    DemoSession,
    PaymentAttemptObservation,
    RecoveryCase,
    WebhookEvent,
)
from leakproof.providers import ProviderError
from leakproof.providers.factory import (
    get_case_insight_provider,
    get_cohort_analysis_provider,
    get_email_provider,
    get_payment_provider,
)
from leakproof.sensors.pollers import (
    poll_checkout_abandonment,
    poll_invoice_aging,
    poll_subscription_health,
    reconcile_provider_events,
)
from leakproof.sensors.processor import process_stored_webhook

settings = get_settings()
celery = Celery("leakproof", broker=settings.redis_url, backend=settings.redis_url)
celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    timezone="Asia/Kolkata",
    beat_schedule={
        "dispatch-unprocessed-webhooks": {
            "task": "leakproof.dispatch_unprocessed_webhooks",
            "schedule": 60.0,
        },
        "dispatch-due-actions": {
            "task": "leakproof.dispatch_due_actions",
            "schedule": 30.0,
        },
        "dispatch-due-demo-abandonments": {
            "task": "leakproof.dispatch_due_demo_abandonments",
            "schedule": 15.0,
        },
        "dispatch-pending-case-insights": {
            "task": "leakproof.dispatch_pending_case_insights",
            "schedule": 15.0,
        },
        "cohort-scan-10m": {
            "task": "leakproof.scan_failure_cohorts",
            "schedule": 600.0,
        },
        "checkout-abandonment-5m": {
            "task": "leakproof.poll_checkout_abandonment",
            "schedule": 300.0,
        },
        "subscription-health-hourly": {
            "task": "leakproof.poll_subscription_health",
            "schedule": 3600.0,
        },
        "provider-reconciler-hourly": {
            "task": "leakproof.reconcile_provider_events",
            "schedule": 3600.0,
        },
        "invoice-aging-daily": {
            "task": "leakproof.poll_invoice_aging",
            "schedule": crontab(hour=7, minute=0),
        },
    },
)


@celery.task(
    name="leakproof.process_webhook",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=5,
)
def process_webhook(webhook_id: int) -> str | None:
    with SessionLocal() as session:
        case_id = process_stored_webhook(session, webhook_id)
        if case_id is not None and _prepare_case_insight(session, case_id):
            run_case_insight.delay(case_id)
        return case_id


def _prepare_case_insight(session, case_id: str) -> bool:
    case = session.get(RecoveryCase, case_id)
    if case is None:
        return False
    demo = session.scalar(
        select(DemoSession).where(
            DemoSession.merchant_id == case.merchant_id,
            DemoSession.customer_id == case.customer_id,
        )
    )
    if demo is None:
        return False
    diagnose_case(session, case.id)
    schedule_demo_recovery_email(session, case.id, settings=get_settings())
    mark_case_insight_pending(session, case.id)
    session.commit()
    return True


@celery.task(name="leakproof.dispatch_unprocessed_webhooks")
def dispatch_unprocessed_webhooks(limit: int = 100) -> int:
    with SessionLocal() as session:
        ids = list(
            session.scalars(
                select(WebhookEvent.id)
                .where(WebhookEvent.processed_at.is_(None))
                .order_by(WebhookEvent.received_at)
                .limit(limit)
            )
        )
    for webhook_id in ids:
        process_webhook.delay(webhook_id)
    return len(ids)


@celery.task(
    name="leakproof.execute_action",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=5,
)
def run_action(action_id: str) -> dict:
    with SessionLocal() as session:
        action = session.get(Action, action_id)
        case = session.get(RecoveryCase, action.case_id) if action is not None else None
        demo = (
            session.scalar(
                select(DemoSession).where(
                    DemoSession.merchant_id == case.merchant_id,
                    DemoSession.customer_id == case.customer_id,
                )
            )
            if case is not None
            else None
        )
        if action is not None and action.action_type == "email_link" and demo is not None:
            return execute_demo_recovery_email(
                session,
                action_id,
                provider=get_email_provider(),
                settings=get_settings(),
            ).__dict__
        return execute_action(session, action_id).__dict__


@celery.task(name="leakproof.dispatch_due_actions")
def dispatch_due_actions(limit: int = 100) -> int:
    with SessionLocal() as session:
        ids = due_action_ids(session, limit=limit)
    for action_id in ids:
        run_action.delay(action_id)
    return len(ids)


@celery.task(
    name="leakproof.check_demo_abandonment",
    autoretry_for=(ProviderError,),
    retry_backoff=True,
    max_retries=5,
)
def check_demo_abandonment(session_id: str, dismissal_event_id: int) -> str | None:
    with SessionLocal() as session:
        case_id = materialize_checkout_abandonment(
            session,
            session_id,
            dismissal_event_id,
            provider=get_payment_provider(),
            settings=get_settings(),
        )
        if case_id is not None and _prepare_case_insight(session, case_id):
            run_case_insight.delay(case_id)
        return case_id


@celery.task(name="leakproof.dispatch_due_demo_abandonments")
def dispatch_due_demo_abandonments(limit: int = 100) -> int:
    with SessionLocal() as session:
        checks = due_abandonment_checks(session, settings=get_settings(), limit=limit)
    dispatched = 0
    for session_id, dismissal_event_id in checks:
        try:
            check_demo_abandonment.delay(session_id, dismissal_event_id)
            dispatched += 1
        except Exception:
            logging.getLogger(__name__).exception("Abandonment dispatch failed; retry on next beat")
    return dispatched


@celery.task(name="leakproof.generate_case_insight")
def run_case_insight(case_id: str) -> str:
    with SessionLocal() as session:
        record = generate_case_insight(
            session,
            case_id,
            provider=get_case_insight_provider(),
            settings=get_settings(),
        )
        return record.status


@celery.task(name="leakproof.dispatch_pending_case_insights")
def dispatch_pending_case_insights(limit: int = 100) -> int:
    with SessionLocal() as session:
        ids = list(
            session.scalars(
                select(CaseInsightRecord.case_id)
                .where(CaseInsightRecord.status == "pending")
                .order_by(CaseInsightRecord.created_at)
                .limit(limit)
            )
        )
    for case_id in ids:
        run_case_insight.delay(case_id)
    return len(ids)


@celery.task(name="leakproof.scan_failure_cohorts")
def scan_failure_cohorts() -> dict[str, dict]:
    window_to = datetime.now(UTC)
    window_from = window_to - timedelta(minutes=20)
    with SessionLocal() as session:
        merchant_ids = list(
            session.scalars(
                select(PaymentAttemptObservation.merchant_id)
                .where(
                    PaymentAttemptObservation.namespace == "live",
                    PaymentAttemptObservation.observed_at >= window_from,
                    PaymentAttemptObservation.observed_at < window_to,
                )
                .distinct()
            )
        )
        provider = get_cohort_analysis_provider()
        return {
            merchant_id: run_cohort_scan(
                session,
                merchant_id=merchant_id,
                window_from=window_from,
                window_to=window_to,
                provider=provider,
            ).__dict__
            for merchant_id in merchant_ids
        }


@celery.task(name="leakproof.poll_checkout_abandonment")
def checkout_poll() -> dict:
    return poll_checkout_abandonment().__dict__


@celery.task(name="leakproof.poll_invoice_aging")
def invoice_poll() -> dict:
    return poll_invoice_aging().__dict__


@celery.task(name="leakproof.poll_subscription_health")
def subscription_poll() -> dict:
    return poll_subscription_health().__dict__


@celery.task(name="leakproof.reconcile_provider_events")
def provider_reconcile() -> dict:
    return reconcile_provider_events().__dict__
