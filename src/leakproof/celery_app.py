from __future__ import annotations

from datetime import UTC, datetime, timedelta

from celery import Celery
from celery.schedules import crontab
from sqlalchemy import select

from leakproof.actuators import due_action_ids, execute_action
from leakproof.config import get_settings
from leakproof.db import SessionLocal
from leakproof.demo.service import due_abandonment_checks, materialize_checkout_abandonment
from leakproof.diagnosis.tier2 import run_cohort_scan
from leakproof.models.db import RecoveryCase, WebhookEvent
from leakproof.providers import ProviderError
from leakproof.providers.factory import get_payment_provider
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
        return process_stored_webhook(session, webhook_id)


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
        return materialize_checkout_abandonment(
            session,
            session_id,
            dismissal_event_id,
            provider=get_payment_provider(),
            settings=get_settings(),
        )


@celery.task(name="leakproof.dispatch_due_demo_abandonments")
def dispatch_due_demo_abandonments(limit: int = 100) -> int:
    with SessionLocal() as session:
        checks = due_abandonment_checks(session, settings=get_settings(), limit=limit)
    for session_id, dismissal_event_id in checks:
        check_demo_abandonment.delay(session_id, dismissal_event_id)
    return len(checks)


@celery.task(name="leakproof.scan_failure_cohorts")
def scan_failure_cohorts() -> dict[str, dict]:
    window_to = datetime.now(UTC)
    window_from = window_to - timedelta(minutes=20)
    with SessionLocal() as session:
        merchant_ids = list(
            session.scalars(
                select(RecoveryCase.merchant_id)
                .where(
                    RecoveryCase.detected_at >= window_from,
                    RecoveryCase.detected_at < window_to,
                )
                .distinct()
            )
        )
        return {
            merchant_id: run_cohort_scan(
                session,
                merchant_id=merchant_id,
                window_from=window_from,
                window_to=window_to,
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
