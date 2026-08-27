from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from leakproof.actuators import due_action_ids, execute_action
from leakproof.diagnosis import diagnose_case
from leakproof.models.db import Action, ActuatorReceipt, Contact, Event, WebhookEvent
from leakproof.models.domain import CaseOutcome, CaseState, LeakType
from leakproof.policy import plan_case
from leakproof.sensors.processor import process_stored_webhook
from leakproof.sensors.webhooks import persist_webhook
from leakproof.services import NormalizedSignal, record_signal

NOW = datetime(2026, 8, 29, 4, 30, tzinfo=UTC)  # 10:00 IST


def _failed_payment(*, suffix: str, failure_class: str = "TRANSIENT") -> NormalizedSignal:
    evidence_by_class = {
        "TRANSIENT": {
            "error_source": "bank",
            "error_step": "payment_authorization",
            "error_reason": "gateway_technical_error",
        },
        "INSTRUMENT_DEAD": {
            "error_source": "customer",
            "error_step": "payment_authorization",
            "error_reason": "card_expired",
        },
    }
    return NormalizedSignal(
        merchant_id=f"merchant_{suffix}",
        customer_id=f"customer_{suffix}",
        leak_type=LeakType.PAYMENT_FAILURE,
        entity_type="payment",
        entity_id=f"pay_failed_{suffix}",
        entity_root_id=f"order_{suffix}",
        amount_at_risk=1_000_000,
        currency="INR",
        evidence=evidence_by_class[failure_class],
        occurred_at=NOW,
    )


def _planned_case(session, *, suffix: str, failure_class: str = "TRANSIENT"):
    case, _ = record_signal(
        session,
        _failed_payment(suffix=suffix, failure_class=failure_class),
    )
    diagnosis = diagnose_case(session, case.id)
    assert diagnosis.failure_class == failure_class
    plan = plan_case(session, case.id, now=NOW)
    session.commit()
    return case, plan


def test_plan_materializes_one_pending_schedule_row_per_step(session_factory):
    with session_factory() as session:
        case, plan = _planned_case(session, suffix="schedule")
        actions = list(
            session.scalars(
                select(Action).where(Action.case_id == case.id).order_by(Action.step_index)
            )
        )

        assert len(actions) == len(plan.steps) == 3
        assert [item.status for item in actions] == ["pending"] * 3
        assert [item.scheduled_for for item in actions] == [
            step.scheduled_for.replace(tzinfo=None) for step in plan.steps
        ]
        assert len({item.id for item in actions}) == 3


def test_due_dispatch_only_selects_pending_actions_at_or_before_now(session_factory):
    with session_factory() as session:
        case, plan = _planned_case(session, suffix="due")
        first_due = plan.steps[0].scheduled_for

        assert due_action_ids(session, now=first_due - timedelta(seconds=1)) == []
        assert due_action_ids(session, now=first_due) == [
            session.scalar(
                select(Action.id).where(
                    Action.case_id == case.id,
                    Action.step_index == 0,
                )
            )
        ]


def test_duplicate_worker_delivery_executes_and_charges_once(session_factory):
    with session_factory() as session:
        case, plan = _planned_case(session, suffix="redelivery")
        action = session.scalar(
            select(Action).where(Action.case_id == case.id, Action.step_index == 0)
        )
        first = execute_action(session, action.id, now=plan.steps[0].scheduled_for)
        duplicate = execute_action(
            session,
            action.id,
            now=plan.steps[0].scheduled_for + timedelta(minutes=1),
        )

        assert first.status == "succeeded"
        assert duplicate.status == "succeeded"
        assert duplicate.replayed is True
        assert duplicate.provider_ref == first.provider_ref
        assert action.attempt_count == 1
        assert case.attribution_until == (
            plan.steps[0].scheduled_for + timedelta(days=7)
        )
        assert session.scalar(select(func.count()).select_from(ActuatorReceipt)) == 1
        assert session.scalar(
            select(func.count()).select_from(Event).where(
                Event.case_id == case.id, Event.kind == "ACTED"
            )
        ) == 1


def test_replayed_webhook_reaches_one_case_schedule_and_one_charge(session_factory):
    payload = {
        "event": "payment.failed",
        "created_at": int(NOW.timestamp()),
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_webhook_once",
                    "order_id": "order_webhook_once",
                    "customer_id": "customer_webhook_once",
                    "amount": 1_000_000,
                    "currency": "INR",
                    "error_source": "bank",
                    "error_step": "payment_authorization",
                    "error_reason": "gateway_technical_error",
                }
            }
        },
    }
    with session_factory() as session:
        first = persist_webhook(
            session,
            merchant_id="merchant_webhook_once",
            payload=payload,
            header_event_id="evt_webhook_once",
        )
        replay = persist_webhook(
            session,
            merchant_id="merchant_webhook_once",
            payload=payload,
            header_event_id="evt_webhook_once",
        )
        case_id = process_stored_webhook(session, first.id)
        assert process_stored_webhook(session, replay.id) is None
        diagnose_case(session, case_id)
        plan = plan_case(session, case_id, now=NOW)
        session.commit()
        action = session.scalar(
            select(Action).where(Action.case_id == case_id, Action.step_index == 0)
        )

        execute_action(session, action.id, now=plan.steps[0].scheduled_for)
        execute_action(session, action.id, now=plan.steps[0].scheduled_for)

        assert replay.duplicate is True
        assert replay.id == first.id
        assert session.scalar(select(func.count()).select_from(WebhookEvent)) == 1
        assert session.scalar(select(func.count()).select_from(ActuatorReceipt)) == 1
        assert session.scalar(
            select(func.count()).select_from(Event).where(
                Event.case_id == case_id, Event.kind == "ACTED"
            )
        ) == 1


def test_customer_message_uses_registered_template_and_records_contact(session_factory):
    with session_factory() as session:
        case, plan = _planned_case(
            session,
            suffix="message",
            failure_class="INSTRUMENT_DEAD",
        )
        action = session.scalar(
            select(Action).where(Action.case_id == case.id, Action.step_index == 0)
        )
        result = execute_action(session, action.id, now=plan.steps[0].scheduled_for)
        receipt = session.get(ActuatorReceipt, action.idempotency_key)

        assert result.status == "succeeded"
        assert receipt.request["message"]["template_id"] == "util_recovery_in_app_v1"
        assert receipt.request["message"]["registration_ref"]
        assert receipt.request.get("body") is None
        assert session.scalar(select(func.count()).select_from(Contact)) == 1


def test_quiet_hours_are_rescheduled_without_calling_an_actuator(session_factory):
    late = datetime(2026, 8, 29, 17, 30, tzinfo=UTC)  # 23:00 IST
    with session_factory() as session:
        case, _ = _planned_case(
            session,
            suffix="quiet",
            failure_class="INSTRUMENT_DEAD",
        )
        action = session.scalar(
            select(Action).where(Action.case_id == case.id, Action.step_index == 0)
        )
        action.scheduled_for = late
        session.commit()

        result = execute_action(session, action.id, now=late)

        assert result.status == "rescheduled"
        assert action.status == "pending"
        assert action.scheduled_for.hour == 8
        assert action.scheduled_for.minute == 0
        assert session.scalar(select(func.count()).select_from(ActuatorReceipt)) == 0


def test_paid_webhook_closes_case_and_cancels_all_later_actions(session_factory):
    with session_factory() as session:
        case, plan = _planned_case(session, suffix="paid")
        first = session.scalar(
            select(Action).where(Action.case_id == case.id, Action.step_index == 0)
        )
        execute_action(session, first.id, now=plan.steps[0].scheduled_for)
        webhook = WebhookEvent(
            merchant_id=case.merchant_id,
            provider="razorpay",
            provider_event_key="evt_paid_once",
            event_type="payment.captured",
            payload={
                "event": "payment.captured",
                "created_at": int((NOW + timedelta(hours=7)).timestamp()),
                "payload": {
                    "payment": {
                        "entity": {
                            "id": "pay_captured_paid",
                            "order_id": "order_paid",
                            "customer_id": "customer_paid",
                            "amount": 1_000_000,
                            "currency": "INR",
                            "status": "captured",
                        }
                    }
                },
            },
            signature_verified=True,
        )
        session.add(webhook)
        session.commit()

        processed_case_id = process_stored_webhook(session, webhook.id)
        actions = list(
            session.scalars(
                select(Action).where(Action.case_id == case.id).order_by(Action.step_index)
            )
        )
        kinds = list(
            session.scalars(
                select(Event.kind).where(Event.case_id == case.id).order_by(Event.seq)
            )
        )

        assert processed_case_id == case.id
        assert case.state == CaseState.CLOSED
        assert case.outcome == CaseOutcome.RECOVERED
        assert [item.status for item in actions] == ["succeeded", "cancelled", "cancelled"]
        assert kinds[-2:] == ["VERIFYING", "CLOSED"]
        assert due_action_ids(session, now=NOW + timedelta(days=30)) == []


def test_redelivered_paid_webhook_does_not_duplicate_terminal_events(session_factory):
    with session_factory() as session:
        case, _ = _planned_case(session, suffix="paid_replay")
        webhook = WebhookEvent(
            merchant_id=case.merchant_id,
            provider="razorpay",
            provider_event_key="evt_paid_replay",
            event_type="order.paid",
            payload={
                "event": "order.paid",
                "payload": {
                    "order": {
                        "entity": {
                            "id": "order_paid_replay",
                            "customer_id": "customer_paid_replay",
                            "amount_paid": 1_000_000,
                            "currency": "INR",
                            "status": "paid",
                        }
                    }
                },
            },
            signature_verified=True,
        )
        session.add(webhook)
        session.commit()

        assert process_stored_webhook(session, webhook.id) == case.id
        assert process_stored_webhook(session, webhook.id) is None
        assert session.scalar(
            select(func.count()).select_from(Event).where(
                Event.case_id == case.id,
                Event.kind == "CLOSED",
            )
        ) == 1
