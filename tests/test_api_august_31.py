from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from leakproof.audit.timeline import replay_case
from leakproof.config import Settings, get_settings
from leakproof.demo import CheckoutEventRequest, DemoSessionCreateRequest, DemoSessionState
from leakproof.demo.email import schedule_demo_recovery_email
from leakproof.demo.rate_limit import InMemoryRateLimiter
from leakproof.demo.security import issue_recovery_token
from leakproof.demo.service import (
    RecoveryExpired,
    RecoveryOrderNotAvailable,
    RecoveryTokenInvalid,
    create_demo_session,
    get_recovery_bootstrap,
    ingest_checkout_event,
    issue_demo_recovery_token,
    materialize_checkout_abandonment,
)
from leakproof.diagnosis import diagnose_case
from leakproof.models.db import Action, DemoSession, Diagnosis, Event, RecoveryCase
from leakproof.providers import Payment
from leakproof.providers.fakes import FakePaymentProvider
from leakproof.sensors.processor import process_stored_webhook
from leakproof.sensors.webhooks import persist_webhook

NOW = datetime(2026, 8, 31, 6, 0, tzinfo=UTC)


def settings() -> Settings:
    return Settings(
        _env_file=None,
        mode="simulation",
        default_merchant_id="merchant_live_demo",
        recovery_token_secret="august-31-recovery-secret-long-enough",
        razorpay_key_id="rzp_test_august_31",
        demo_session_ttl_minutes=30,
        demo_abandonment_delay_seconds=30,
    )


def create_session(session):
    provider = FakePaymentProvider()
    config = settings()
    created = create_demo_session(
        session,
        DemoSessionCreateRequest(),
        client_ip="203.0.113.31",
        provider=provider,
        limiter=InMemoryRateLimiter(),
        settings=config,
        now=NOW,
    )
    return created, provider, config


def razorpay_payload(
    event_type: str,
    order_id: str,
    *,
    payment_id: str = "pay_demo_1",
    occurred_at: datetime = NOW,
) -> dict:
    if event_type == "order.paid":
        return {
            "event": event_type,
            "created_at": int(occurred_at.timestamp()),
            "payload": {
                "order": {
                    "entity": {
                        "id": order_id,
                        "amount": 50_000,
                        "amount_paid": 50_000,
                        "currency": "INR",
                        "status": "paid",
                    }
                }
            },
        }
    entity = {
        "id": payment_id,
        "order_id": order_id,
        "amount": 50_000,
        "currency": "INR",
        "status": "failed" if event_type == "payment.failed" else "captured",
    }
    if event_type == "payment.failed":
        entity.update(
            {
                "error_source": "bank",
                "error_step": "payment_authorization",
                "error_reason": "gateway_technical_error",
            }
        )
    return {
        "event": event_type,
        "created_at": int(occurred_at.timestamp()),
        "payload": {"payment": {"entity": entity}},
    }


def process_payload(session, merchant_id: str, payload: dict, event_id: str) -> str | None:
    ingested = persist_webhook(
        session,
        merchant_id=merchant_id,
        payload=payload,
        header_event_id=event_id,
    )
    return process_stored_webhook(session, ingested.id)


def create_abandonment(session, created, provider, config) -> str:
    dismissed = ingest_checkout_event(
        session,
        created.session_id,
        CheckoutEventRequest.model_validate(
            {
                "client_event_id": "dismiss-31",
                "event_type": "checkout_dismissed",
                "occurred_at": (NOW + timedelta(seconds=1)).isoformat(),
                "metadata": {"dismissed_by": "customer"},
            }
        ),
        session_token=created.session_token,
        limiter=InMemoryRateLimiter(),
        settings=config,
        now=NOW + timedelta(seconds=1),
    )
    case_id = materialize_checkout_abandonment(
        session,
        created.session_id,
        dismissed.dismissal_event_id,
        provider=provider,
        settings=config,
        now=NOW + timedelta(seconds=31),
    )
    assert case_id is not None
    return case_id


def test_api_failure_after_abandonment_promotes_same_case_without_webhook(session_factory):
    with session_factory() as session:
        created, provider, config = create_session(session)
        case_id = create_abandonment(session, created, provider, config)
        diagnose_case(session, case_id)
        email = schedule_demo_recovery_email(session, case_id, settings=config, now=NOW)
        limiter = InMemoryRateLimiter()
        for event_type, seconds in (("payment_attempt_started", 40), ("checkout_dismissed", 41)):
            ingested = ingest_checkout_event(
                session,
                created.session_id,
                CheckoutEventRequest(
                    client_event_id=f"retry-{event_type}",
                    event_type=event_type,
                    occurred_at=NOW + timedelta(seconds=seconds),
                ),
                session_token=created.session_token,
                limiter=limiter,
                settings=config,
                now=NOW + timedelta(seconds=seconds),
            )
        provider.payments["pay_retry_failed"] = Payment(
            id="pay_retry_failed",
            order_id=created.razorpay_order_id,
            amount_paise=created.amount_paise,
            currency=created.currency,
            status="failed",
            method="card",
        )

        for seconds in (71, 72):
            assert materialize_checkout_abandonment(
                session,
                created.session_id,
                ingested.dismissal_event_id,
                provider=provider,
                settings=config,
                now=NOW + timedelta(seconds=seconds),
            ) == case_id

        case = session.get(RecoveryCase, case_id)
        assert case.leak_type == "PAYMENT_FAILURE"
        assert case.entity_id == "pay_retry_failed"
        assert session.get(Diagnosis, case_id).evidence["source"] == "razorpay_payment_api"
        assert session.scalar(select(func.count()).select_from(RecoveryCase)) == 1
        assert session.scalar(
            select(func.count()).select_from(Event).where(Event.kind == "RECLASSIFIED")
        ) == 1
        assert replay_case(session, case_id).projection_matches
        assert schedule_demo_recovery_email(session, case_id, settings=config).id == email.id
        assert session.scalar(select(func.count()).select_from(Action)) == 1

        assert process_payload(
            session,
            config.default_merchant_id,
            razorpay_payload(
                "payment.captured",
                created.razorpay_order_id,
                occurred_at=NOW + timedelta(seconds=80),
            ),
            "evt-captured-after-api-promotion",
        ) == case_id
        assert case.state == "CLOSED"
        assert case.outcome == "RECOVERED"
        assert email.status == "cancelled"
        assert replay_case(session, case_id).projection_matches


def test_payment_failure_creates_one_live_case_and_duplicate_has_one_effect(session_factory):
    with session_factory() as session:
        created, _, _ = create_session(session)
        payload = razorpay_payload("payment.failed", created.razorpay_order_id)

        first = persist_webhook(
            session,
            merchant_id="merchant_live_demo",
            payload=payload,
            header_event_id="evt-failed-once",
        )
        duplicate = persist_webhook(
            session,
            merchant_id="merchant_live_demo",
            payload=payload,
            header_event_id="evt-failed-once",
        )
        case_id = process_stored_webhook(session, first.id)
        assert process_stored_webhook(session, duplicate.id) is None

        case = session.get(RecoveryCase, case_id)
        demo = session.get(DemoSession, created.session_id)
        assert duplicate.duplicate is True
        assert case.leak_type == "PAYMENT_FAILURE"
        assert case.dedupe_key == f"live:{created.session_id}:{created.razorpay_order_id}"
        assert case.customer_id == demo.customer_id
        assert demo.state == DemoSessionState.AT_RISK
        assert session.scalar(select(func.count()).select_from(RecoveryCase)) == 1
        assert session.scalar(select(func.count()).select_from(Event)) == 2


def test_failure_promotes_abandonment_instead_of_creating_a_second_case(session_factory):
    with session_factory() as session:
        created, provider, config = create_session(session)
        abandonment_id = create_abandonment(session, created, provider, config)

        failure_id = process_payload(
            session,
            "merchant_live_demo",
            razorpay_payload("payment.failed", created.razorpay_order_id),
            "evt-promote-failure",
        )
        case = session.get(RecoveryCase, abandonment_id)
        kinds = list(
            session.scalars(
                select(Event.kind).where(Event.case_id == abandonment_id).order_by(Event.seq)
            )
        )

        assert failure_id == abandonment_id
        assert case.leak_type == "PAYMENT_FAILURE"
        assert case.entity_type == "payment"
        assert session.scalar(select(func.count()).select_from(RecoveryCase)) == 1
        assert kinds == ["DETECTED", "ASSIGNED", "SIGNAL", "RECLASSIFIED"]
        assert replay_case(session, abandonment_id).projection_matches is True


def test_captured_payment_closes_an_abandonment_case_by_original_order(session_factory):
    with session_factory() as session:
        created, provider, config = create_session(session)
        abandonment_id = create_abandonment(session, created, provider, config)

        closed_id = process_payload(
            session,
            "merchant_live_demo",
            razorpay_payload(
                "payment.captured",
                created.razorpay_order_id,
                occurred_at=NOW + timedelta(minutes=2),
            ),
            "evt-captured-after-abandonment",
        )
        case = session.get(RecoveryCase, abandonment_id)

        assert closed_id == abandonment_id
        assert case.state == "CLOSED"
        assert case.outcome == "RECOVERED"
        assert session.get(DemoSession, created.session_id).state == DemoSessionState.RECOVERED


@pytest.mark.parametrize("success_type", ["payment.captured", "order.paid"])
def test_success_closes_same_case_and_cancels_pending_actions(
    session_factory, success_type: str
):
    with session_factory() as session:
        created, _, _ = create_session(session)
        case_id = process_payload(
            session,
            "merchant_live_demo",
            razorpay_payload("payment.failed", created.razorpay_order_id),
            f"evt-failure-before-{success_type}",
        )
        session.add(
            Action(
                id=f"action-{success_type}",
                case_id=case_id,
                step_index=0,
                action_type="email_link",
                scheduled_for=NOW + timedelta(minutes=1),
                idempotency_key=f"action-idem-{success_type}",
                status="pending",
            )
        )
        session.commit()

        closed_id = process_payload(
            session,
            "merchant_live_demo",
            razorpay_payload(
                success_type,
                created.razorpay_order_id,
                occurred_at=NOW + timedelta(minutes=2),
            ),
            f"evt-success-{success_type}",
        )
        case = session.get(RecoveryCase, case_id)

        assert closed_id == case_id
        assert case.state == "CLOSED"
        assert case.outcome == "RECOVERED"
        assert session.get(DemoSession, created.session_id).state == DemoSessionState.RECOVERED
        assert session.get(Action, f"action-{success_type}").status == "cancelled"
        assert list(
            session.scalars(
                select(Event.kind).where(Event.case_id == case_id).order_by(Event.seq)
            )
        )[-2:] == ["VERIFYING", "CLOSED"]


def test_delayed_failure_reconciles_against_success_processed_first(session_factory):
    with session_factory() as session:
        created, _, _ = create_session(session)
        success_at = NOW + timedelta(seconds=10)
        assert (
            process_payload(
                session,
                "merchant_live_demo",
                razorpay_payload(
                    "payment.captured",
                    created.razorpay_order_id,
                    occurred_at=success_at,
                ),
                "evt-success-first",
            )
            is None
        )
        assert session.scalar(select(func.count()).select_from(RecoveryCase)) == 0

        case_id = process_payload(
            session,
            "merchant_live_demo",
            razorpay_payload(
                "payment.failed",
                created.razorpay_order_id,
                occurred_at=NOW + timedelta(seconds=1),
            ),
            "evt-delayed-failure",
        )
        case = session.get(RecoveryCase, case_id)

        assert case.leak_type == "PAYMENT_FAILURE"
        assert case.state == "CLOSED"
        assert case.outcome == "RECOVERED"
        assert session.scalar(select(func.count()).select_from(RecoveryCase)) == 1


def test_recovery_token_is_bound_and_bootstraps_only_the_original_unpaid_order(
    session_factory,
):
    with session_factory() as session:
        created, provider, config = create_session(session)
        process_payload(
            session,
            "merchant_live_demo",
            razorpay_payload("payment.failed", created.razorpay_order_id),
            "evt-failure-for-recovery",
        )
        token = issue_demo_recovery_token(
            session, created.session_id, settings=config, now=NOW + timedelta(seconds=1)
        )

        bootstrap = get_recovery_bootstrap(
            session,
            token,
            provider=provider,
            settings=config,
            now=NOW + timedelta(seconds=2),
        )

        assert bootstrap.session_id == created.session_id
        assert bootstrap.razorpay_order_id == created.razorpay_order_id
        assert bootstrap.amount_paise == created.amount_paise
        assert session.get(DemoSession, created.session_id).state == DemoSessionState.CHECKOUT_OPEN

        with pytest.raises(RecoveryTokenInvalid):
            get_recovery_bootstrap(
                session,
                token[:-1] + ("A" if token[-1] != "A" else "B"),
                provider=provider,
                settings=config,
                now=NOW + timedelta(seconds=2),
            )

        wrong_amount = issue_recovery_token(
            created.session_id,
            "merchant_live_demo",
            created.razorpay_order_id,
            created.amount_paise + 1,
            "INR",
            NOW + timedelta(minutes=30),
            config.recovery_token_secret,
        )
        with pytest.raises(RecoveryTokenInvalid):
            get_recovery_bootstrap(
                session,
                wrong_amount,
                provider=provider,
                settings=config,
                now=NOW + timedelta(seconds=2),
            )


def test_expired_token_and_paid_order_fail_closed(session_factory):
    with session_factory() as session:
        created, provider, config = create_session(session)
        demo = session.get(DemoSession, created.session_id)
        demo.state = DemoSessionState.AT_RISK.value
        session.commit()

        expired = issue_recovery_token(
            demo.id,
            demo.merchant_id,
            demo.razorpay_order_id,
            demo.amount_paise,
            demo.currency,
            NOW + timedelta(seconds=1),
            config.recovery_token_secret,
        )
        with pytest.raises(RecoveryExpired):
            get_recovery_bootstrap(
                session,
                expired,
                provider=provider,
                settings=config,
                now=NOW + timedelta(seconds=2),
            )

        valid = issue_demo_recovery_token(
            session, demo.id, settings=config, now=NOW + timedelta(seconds=2)
        )
        provider.payments["pay_authorized"] = Payment(
            id="pay_authorized",
            order_id=demo.razorpay_order_id,
            amount_paise=demo.amount_paise,
            currency=demo.currency,
            status="captured",
        )
        with pytest.raises(RecoveryOrderNotAvailable):
            get_recovery_bootstrap(
                session,
                valid,
                provider=provider,
                settings=config,
                now=NOW + timedelta(seconds=3),
            )


def test_recovery_route_returns_checkout_bootstrap(client, session_factory):
    created = client.post("/demo/sessions", json={}).json()
    with session_factory() as session:
        demo = session.get(DemoSession, created["session_id"])
        demo.state = DemoSessionState.AT_RISK.value
        session.commit()
        token = issue_demo_recovery_token(
            session,
            demo.id,
            settings=get_settings(),
            now=datetime.now(UTC),
        )

    response = client.get(f"/recover/{token}")

    assert response.status_code == 200
    assert response.json()["session_id"] == created["session_id"]
    assert response.json()["razorpay_order_id"] == created["razorpay_order_id"]
    assert response.json()["amount_paise"] == created["amount_paise"]


@pytest.mark.parametrize("finish_via", ["worker", "recovery_bootstrap"])
def test_capture_recheck_closes_same_case_and_cancels_contact(session_factory, finish_via):
    from leakproof.demo.service import due_abandonment_checks

    with session_factory() as session:
        created, provider, config = create_session(session)
        case_id = create_abandonment(session, created, provider, config)
        diagnose_case(session, case_id)
        email = schedule_demo_recovery_email(session, case_id, settings=config, now=NOW)
        # A new dismissal is pending while the original abandonment case is already at risk.
        dismissed = ingest_checkout_event(
            session,
            created.session_id,
            CheckoutEventRequest(
                client_event_id="new-dismissal", event_type="checkout_dismissed", occurred_at=NOW
            ),
            session_token=created.session_token,
            limiter=InMemoryRateLimiter(),
            settings=config,
            now=NOW + timedelta(seconds=40),
        )
        due = NOW + timedelta(seconds=71)
        assert due_abandonment_checks(session, settings=config, now=due) == [
            (created.session_id, dismissed.dismissal_event_id)
        ]
        provider.payments["pay_capture_recheck"] = Payment(
            id="pay_capture_recheck",
            order_id=created.razorpay_order_id,
            amount_paise=created.amount_paise,
            currency="INR",
            status="authorized",
        )
        assert (
            materialize_checkout_abandonment(
                session,
                created.session_id,
                dismissed.dismissal_event_id,
                provider=provider,
                settings=config,
                now=due,
            )
            is None
        )
        from leakproof.demo.projection import get_demo_session_projection

        projection = get_demo_session_projection(
            session,
            created.session_id,
            session_token=created.session_token,
            settings=config,
            now=due,
        )
        assert projection.abandonment_check.status == "provider_pending"
        assert projection.recovery_path is None
        assert email.status == "pending"
        assert due_abandonment_checks(session, settings=config, now=due)
        provider.payments["pay_capture_recheck"] = Payment(
            id="pay_capture_recheck",
            order_id=created.razorpay_order_id,
            amount_paise=created.amount_paise,
            currency="INR",
            status="captured",
        )
        if finish_via == "worker":
            materialize_checkout_abandonment(
                session,
                created.session_id,
                dismissed.dismissal_event_id,
                provider=provider,
                settings=config,
                now=due,
            )
        else:
            token = issue_demo_recovery_token(session, created.session_id, settings=config, now=due)
            with pytest.raises(RecoveryOrderNotAvailable):
                get_recovery_bootstrap(session, token, provider=provider, settings=config, now=due)
        assert session.get(DemoSession, created.session_id).state == "RECOVERED"
        assert session.get(RecoveryCase, case_id).state == "CLOSED"
        assert email.status == "cancelled"
        assert not due_abandonment_checks(session, settings=config, now=due)
        assert session.scalar(select(func.count()).select_from(RecoveryCase)) == 1
        assert replay_case(session, case_id).projection_matches
