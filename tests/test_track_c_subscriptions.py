from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from leakproof.config import Settings
from leakproof.demo.acceptance import build_demo_acceptance_export
from leakproof.demo.contracts import DemoSessionCreateRequest
from leakproof.demo.email import execute_demo_recovery_email, schedule_demo_recovery_email
from leakproof.demo.insights import generate_case_insight
from leakproof.demo.rate_limit import InMemoryRateLimiter
from leakproof.demo.service import (
    create_demo_session,
    get_recovery_bootstrap,
    issue_demo_recovery_token,
)
from leakproof.demo.subscriptions import reconcile_subscription, subscription_view
from leakproof.diagnosis import diagnose_case
from leakproof.models.db import (
    Action,
    Customer,
    DemoSession,
    Diagnosis,
    Event,
    ProviderObligation,
    RecoveryCase,
    Settlement,
)
from leakproof.providers import Invoice, Payment, ProviderError
from leakproof.providers.fakes import (
    FakeCaseInsightProvider,
    FakeEmailProvider,
    FakePaymentProvider,
)
from leakproof.sensors.processor import process_stored_webhook
from leakproof.sensors.webhooks import persist_webhook

NOW = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)


def settings(**changes):
    values = dict(
        _env_file=None,
        mode="simulation",
        default_merchant_id="merchant_track_c",
        recovery_token_secret="track-c-recovery-secret-long-enough",
        razorpay_key_id="rzp_test_track_c",
        demo_subscription_plan_id="plan_track_c_reusable",
        demo_session_ttl_minutes=120,
        demo_abandonment_delay_seconds=1,
    )
    values.update(changes)
    return Settings(**values)


def setup_subscription(session, provider, *, config=None, recipient=None):
    config = config or settings()
    created = create_demo_session(
        session,
        DemoSessionCreateRequest(scenario_type="SUBSCRIPTION_HALT", recipient=recipient),
        client_ip="127.0.0.1",
        provider=provider,
        limiter=InMemoryRateLimiter(),
        settings=config,
        now=NOW,
    )
    demo = session.get(DemoSession, created.session_id)
    return created, demo, config


def add_cycle(provider, demo, suffix="1", *, status="issued", due=50_000, paid=0):
    invoice = Invoice(
        id=f"inv_cycle_{suffix}",
        order_id=f"order_cycle_{suffix}",
        subscription_id=demo.primary_entity_id,
        status=status,
        amount_paise=due + paid,
        amount_paid_paise=paid,
        amount_due_paise=due,
        currency="INR",
    )
    provider.invoices[invoice.id] = invoice
    return invoice


def set_subscription(provider, demo, status, method="card"):
    current = provider.subscriptions[demo.primary_entity_id]
    provider.subscriptions[current.id] = current.model_copy(
        update={"status": status, "payment_method": method}
    )


def capture_cycle(provider, invoice, *, payment_id, occurred=NOW + timedelta(minutes=10)):
    provider.invoices[invoice.id] = invoice.model_copy(
        update={"status": "paid", "amount_paid_paise": invoice.amount_paise, "amount_due_paise": 0}
    )
    provider.payments[payment_id] = Payment(
        payment_id,
        invoice.order_id,
        invoice.amount_paise,
        invoice.currency,
        "captured",
        created_at=int(occurred.timestamp()),
        invoice_id=invoice.id,
    )


def mandate_failure_payload(demo, invoice, *, reason="mandate_not_active", method="emandate"):
    return {
        "event": "payment.failed",
        "created_at": int(NOW.timestamp()),
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_mandate_failure",
                    "subscription_id": demo.primary_entity_id,
                    "invoice_id": invoice.id,
                    "method": method,
                    "recurring": True,
                    "error_reason": reason,
                }
            },
            "invoice": {
                "entity": {"id": invoice.id, "subscription_id": demo.primary_entity_id}
            },
            "subscription": {"entity": {"id": demo.primary_entity_id}},
        },
    }


def process_subscription_payload(session, provider, config, payload, event_id, now):
    stored = persist_webhook(
        session,
        merchant_id=config.default_merchant_id,
        payload=payload,
        header_event_id=event_id,
    )
    return process_stored_webhook(
        session, stored.id, provider=provider, settings=config, now=now
    )


def test_setup_uses_one_configured_plan_and_pending_to_halted_updates_same_cycle_case(
    session_factory,
):
    provider = FakePaymentProvider()
    with session_factory() as session:
        created, demo, config = setup_subscription(session, provider)
        assert created.authorization_url.startswith("https://rzp.io/")
        assert provider.subscription_create_calls[0].plan_id == config.demo_subscription_plan_id
        assert provider.subscription_create_calls[0].customer_notify is False
        cycle = add_cycle(provider, demo)
        set_subscription(provider, demo, "pending")
        first = reconcile_subscription(
            session, demo.id, provider=provider, settings=config, now=NOW + timedelta(minutes=1)
        )
        set_subscription(provider, demo, "halted")
        second = reconcile_subscription(
            session, demo.id, provider=provider, settings=config, now=NOW + timedelta(minutes=2)
        )
        assert first.id == second.id
        assert session.scalar(select(func.count(RecoveryCase.id))) == 1
        assert subscription_view(session, demo, NOW)["retry_owner"] == "razorpay"
        obligation = session.scalar(
            select(ProviderObligation).where(ProviderObligation.provider_entity_id == cycle.id)
        )
        assert obligation.case_id == first.id
        events = list(session.scalars(select(Event).where(Event.case_id == first.id)))
        assert any(
            e.kind == "SUBSCRIPTION_RECONCILED" and e.payload["subscription_status"] == "pending"
            for e in events
        )
        assert any(
            e.kind == "SUBSCRIPTION_RECONCILED" and e.payload["subscription_status"] == "halted"
            for e in events
        )


def test_activation_and_future_cycle_payment_do_not_collect_older_arrears(session_factory):
    provider = FakePaymentProvider()
    with session_factory() as session:
        _, demo, config = setup_subscription(session, provider)
        old = add_cycle(provider, demo, "old")
        set_subscription(provider, demo, "halted")
        case = reconcile_subscription(
            session, demo.id, provider=provider, settings=config, now=NOW + timedelta(minutes=1)
        )
        set_subscription(provider, demo, "active")
        reconcile_subscription(
            session, demo.id, provider=provider, settings=config, now=NOW + timedelta(minutes=2)
        )
        assert case.outcome != "RECOVERED"
        assert subscription_view(session, demo, NOW)["disposition"] == "active_with_arrears"
        assert session.scalar(select(func.coalesce(func.sum(Settlement.credited_paise), 0))) == 0

        future = add_cycle(provider, demo, "future")
        capture_cycle(provider, future, payment_id="pay_future_cycle")
        reconcile_subscription(
            session,
            demo.id,
            provider=provider,
            settings=config,
            now=NOW + timedelta(minutes=11),
            explicit_invoice_id=future.id,
        )
        session.refresh(case)
        assert case.outcome != "RECOVERED"
        assert (
            subscription_view(session, demo, NOW)["outstanding_balance_paise"]
            == old.amount_due_paise
        )
        assert session.scalar(select(func.coalesce(func.sum(Settlement.credited_paise), 0))) == 0


def test_exact_invoice_capture_closes_case_and_next_cycle_gets_a_new_case(session_factory):
    provider = FakePaymentProvider()
    with session_factory() as session:
        created, demo, config = setup_subscription(session, provider)
        first_cycle = add_cycle(provider, demo, "one")
        set_subscription(provider, demo, "halted")
        first = reconcile_subscription(
            session, demo.id, provider=provider, settings=config, now=NOW + timedelta(minutes=1)
        )
        diagnose_case(session, first.id)
        capture_cycle(provider, first_cycle, payment_id="pay_cycle_one")
        set_subscription(provider, demo, "active")
        reconcile_subscription(
            session, demo.id, provider=provider, settings=config, now=NOW + timedelta(minutes=11)
        )
        session.refresh(first)
        assert first.outcome == "RECOVERED"
        assert subscription_view(session, demo, NOW)["recovered_paise"] == first_cycle.amount_paise

        add_cycle(provider, demo, "two")
        set_subscription(provider, demo, "pending")
        second = reconcile_subscription(
            session, demo.id, provider=provider, settings=config, now=NOW + timedelta(minutes=20)
        )
        assert second.id != first.id
        assert session.scalar(select(func.count(RecoveryCase.id))) == 2
        assert session.scalar(select(func.count(Settlement.payment_id.distinct()))) == 1


def test_method_update_bootstrap_is_allowlisted_and_intentional_states_disable_it(session_factory):
    provider = FakePaymentProvider()
    with session_factory() as session:
        _, demo, config = setup_subscription(session, provider)
        add_cycle(provider, demo)
        set_subscription(provider, demo, "halted", "card")
        reconcile_subscription(
            session, demo.id, provider=provider, settings=config, now=NOW + timedelta(minutes=1)
        )
        token = issue_demo_recovery_token(
            session, demo.id, settings=config, now=NOW + timedelta(minutes=2)
        )
        bootstrap = get_recovery_bootstrap(
            session, token, provider=provider, settings=config, now=NOW + timedelta(minutes=2)
        )
        assert bootstrap.purpose == "subscription_method_update"
        assert bootstrap.subscription_card_change is True
        assert provider.create_calls == []  # no app-owned debit/order

        set_subscription(provider, demo, "paused", "card")
        reconcile_subscription(
            session, demo.id, provider=provider, settings=config, now=NOW + timedelta(minutes=3)
        )
        assert subscription_view(session, demo, NOW)["method_update_available"] is False
        with pytest.raises(ProviderError, match="unavailable"):
            get_recovery_bootstrap(
                session, token, provider=provider, settings=config, now=NOW + timedelta(minutes=3)
            )


def test_delayed_duplicate_and_mismatched_webhooks_reconcile_current_provider_truth(
    session_factory,
):
    provider = FakePaymentProvider()
    with session_factory() as session:
        _, demo, config = setup_subscription(session, provider)
        cycle = add_cycle(provider, demo)
        set_subscription(provider, demo, "halted")
        payload = {
            "event": "subscription.pending",
            "created_at": int(NOW.timestamp()),
            "payload": {
                "subscription": {"entity": {"id": demo.primary_entity_id, "status": "pending"}},
                "invoice": {"entity": {"id": cycle.id, "subscription_id": demo.primary_entity_id}},
            },
        }
        stored = persist_webhook(
            session,
            merchant_id=demo.merchant_id,
            payload=payload,
            header_event_id="evt_track_c_delayed",
        )
        duplicate = persist_webhook(
            session,
            merchant_id=demo.merchant_id,
            payload=payload,
            header_event_id="evt_track_c_delayed",
        )
        assert duplicate.duplicate is True
        case_id = process_stored_webhook(
            session, stored.id, provider=provider, settings=config, now=NOW + timedelta(minutes=5)
        )
        case = session.get(RecoveryCase, case_id)
        assert (
            case is not None
            and subscription_view(session, demo, NOW)["provider_status"] == "halted"
        )
        assert (
            process_stored_webhook(
                session,
                stored.id,
                provider=provider,
                settings=config,
                now=NOW + timedelta(minutes=6),
            )
            is None
        )

        bad = persist_webhook(
            session,
            merchant_id=demo.merchant_id,
            payload={
                **payload,
                "payload": {
                    **payload["payload"],
                    "invoice": {"entity": {"id": "inv_other", "subscription_id": "sub_other"}},
                },
            },
            header_event_id="evt_track_c_bad",
        )
        with pytest.raises(ProviderError):
            process_stored_webhook(
                session, bad.id, provider=provider, settings=config, now=NOW + timedelta(minutes=7)
            )


def test_provider_model_and_email_failures_degrade_without_revenue_or_extra_debit(session_factory):
    provider = FakePaymentProvider()
    config = settings(
        outbound_email_enabled=True,
        resend_api_key="test",
        resend_from_email="demo@example.com",
        demo_email_allowlist="allowed@example.com",
    )
    with session_factory() as session:
        _, demo, _ = setup_subscription(
            session, provider, config=config, recipient="allowed@example.com"
        )
        add_cycle(provider, demo)
        set_subscription(provider, demo, "halted")
        case = reconcile_subscription(
            session, demo.id, provider=provider, settings=config, now=NOW + timedelta(minutes=1)
        )
        provider.failure = ProviderError(
            "razorpay", "fetch_subscription", "provider_unavailable", True, "down"
        )
        with pytest.raises(ProviderError):
            reconcile_subscription(
                session,
                demo.id,
                provider=provider,
                settings=config,
                now=NOW + timedelta(minutes=2),
            )
        provider.failure = None
        diagnose_case(session, case.id)
        insight = generate_case_insight(
            session,
            case.id,
            provider=FakeCaseInsightProvider(
                failure=ProviderError(
                    "openai", "case_insight", "provider_unavailable", True, "down"
                )
            ),
            settings=config,
        )
        assert insight.status == "fallback"
        action = schedule_demo_recovery_email(session, case.id, settings=config, now=NOW)
        result = execute_demo_recovery_email(
            session,
            action.id,
            provider=FakeEmailProvider(
                failure=ProviderError("resend", "send", "provider_unavailable", True, "down")
            ),
            settings=config,
            now=NOW + timedelta(seconds=2),
            invoice_provider=provider,
        )
        assert result.status == "failed"
        assert case.outcome != "RECOVERED"
        assert provider.create_calls == []
        assert session.scalar(select(func.coalesce(func.sum(Settlement.credited_paise), 0))) == 0


def test_subscription_acceptance_capture_separates_service_and_invoice_recovery(
    session_factory, request
):
    provider = FakePaymentProvider()
    with session_factory() as session:
        created, demo, config = setup_subscription(session, provider)
        cycle = add_cycle(provider, demo)
        set_subscription(provider, demo, "pending")
        case = reconcile_subscription(
            session, demo.id, provider=provider, settings=config, now=NOW + timedelta(minutes=1)
        )
        diagnose_case(session, case.id)
        set_subscription(provider, demo, "halted")
        reconcile_subscription(
            session, demo.id, provider=provider, settings=config, now=NOW + timedelta(minutes=2)
        )
        token = issue_demo_recovery_token(
            session, demo.id, settings=config, now=NOW + timedelta(minutes=3)
        )
        get_recovery_bootstrap(
            session, token, provider=provider, settings=config, now=NOW + timedelta(minutes=3)
        )
        capture_cycle(
            provider, cycle, payment_id="pay_acceptance", occurred=NOW + timedelta(minutes=4)
        )
        set_subscription(provider, demo, "active")
        reconcile_subscription(
            session, demo.id, provider=provider, settings=config, now=NOW + timedelta(minutes=4)
        )
        artifact = build_demo_acceptance_export(
            session,
            demo.id,
            session_token=created.session_token,
            settings=config,
            now=NOW + timedelta(minutes=5),
        )
        checks = {item.check: item.passed for item in artifact.checks}
        assert artifact.passed, [(item.check, item.passed) for item in artifact.checks]
        assert artifact.subscription.retry_owner == "razorpay"
        assert checks["pending_to_halted_same_case"]
        assert checks["exact_invoice_settled"]
        assert checks["recovered_revenue_is_captured"]
        assert checks["no_app_owned_debit"]
        assert checks["captured_payment_globally_unique"]
        output = request.config.getoption("--acceptance-output-dir")
        if output:
            from pathlib import Path

            Path(output).mkdir(parents=True, exist_ok=True)
            Path(output, "subscription-halt.json").write_text(
                artifact.model_dump_json() + "\n"
            )


@pytest.mark.parametrize(
    ("reason", "method"),
    [
        ("insufficient_funds", "emandate"),
        ("payment_mandate_not_active", "emandate"),
        ("mandate_not_active", "card"),
        ("generic_decline", "emandate"),
    ],
)
def test_mandate_false_classification_requires_exact_method_scoped_evidence(
    session_factory, reason, method
):
    provider = FakePaymentProvider()
    with session_factory() as session:
        _, demo, config = setup_subscription(session, provider)
        cycle = add_cycle(provider, demo)
        set_subscription(provider, demo, "halted", method)
        case_id = process_subscription_payload(
            session,
            provider,
            config,
            mandate_failure_payload(demo, cycle, reason=reason, method=method),
            f"evt_false_{reason}_{method}",
            NOW + timedelta(minutes=1),
        )
        assert session.get(RecoveryCase, case_id).leak_type == "SUBSCRIPTION_HALT"


def test_stronger_mandate_evidence_reclassifies_existing_case_and_refreshes_diagnosis(
    session_factory,
):
    provider = FakePaymentProvider()
    with session_factory() as session:
        _, demo, config = setup_subscription(session, provider)
        cycle = add_cycle(provider, demo)
        set_subscription(provider, demo, "halted", "emandate")
        case = reconcile_subscription(
            session, demo.id, provider=provider, settings=config, now=NOW + timedelta(minutes=1)
        )
        diagnose_case(session, case.id)
        action = schedule_demo_recovery_email(session, case.id, settings=config, now=NOW)

        case_id = process_subscription_payload(
            session,
            provider,
            config,
            mandate_failure_payload(demo, cycle),
            "evt_qualified_mandate",
            NOW + timedelta(minutes=2),
        )
        session.refresh(case)
        session.refresh(action)
        diagnosis = session.get(Diagnosis, case.id)
        assert case_id == case.id
        assert case.leak_type == "MANDATE_BROKEN"
        assert diagnosis.failure_class == "INSTRUMENT_DEAD"
        assert diagnosis.evidence["error_reason"] == "mandate_not_active"
        assert action.status == "cancelled"
        assert session.scalar(select(func.count(RecoveryCase.id))) == 1
        assert session.scalar(select(func.count(Action.id))) == 1
        assert session.scalar(select(func.count(Settlement.id))) == 0


def test_duplicate_authorization_repair_is_non_monetary_then_verified_payment_recovers(
    session_factory,
):
    provider = FakePaymentProvider()
    with session_factory() as session:
        _, demo, config = setup_subscription(session, provider)
        cycle = add_cycle(provider, demo)
        set_subscription(provider, demo, "halted", "emandate")
        case_id = process_subscription_payload(
            session,
            provider,
            config,
            mandate_failure_payload(demo, cycle),
            "evt_mandate_failure",
            NOW + timedelta(minutes=1),
        )
        case = session.get(RecoveryCase, case_id)
        set_subscription(provider, demo, "active", "emandate")
        activated = {
            "event": "subscription.activated",
            "payload": {
                "subscription": {"entity": {"id": demo.primary_entity_id}},
                "invoice": {
                    "entity": {"id": cycle.id, "subscription_id": demo.primary_entity_id}
                },
            },
        }
        for suffix in ("one", "two"):
            process_subscription_payload(
                session,
                provider,
                config,
                activated,
                f"evt_repair_{suffix}",
                NOW + timedelta(minutes=2),
            )
        session.refresh(case)
        view = subscription_view(session, demo, NOW + timedelta(minutes=2))
        repair_events = list(
            session.scalars(
                select(Event).where(
                    Event.case_id == case.id,
                    Event.kind == "ENTITY_STATE",
                )
            )
        )
        assert view["authorization_repaired"] is True
        assert sum(e.payload["state"] == "authorization_repaired" for e in repair_events) == 1
        assert case.outcome != "RECOVERED"
        assert session.scalar(select(func.count(Settlement.id))) == 0
        assert view["outstanding_balance_paise"] == cycle.amount_due_paise

        capture_cycle(provider, cycle, payment_id="pay_after_mandate_repair")
        reconcile_subscription(
            session,
            demo.id,
            provider=provider,
            settings=config,
            now=NOW + timedelta(minutes=10),
        )
        session.refresh(case)
        assert case.outcome == "RECOVERED"
        assert subscription_view(session, demo, NOW)["recovered_paise"] == cycle.amount_paise


def test_mandate_contract_acceptance_keeps_repair_and_captured_revenue_separate(
    session_factory, request
):
    provider = FakePaymentProvider()
    with session_factory() as session:
        config = settings(subscription_method_allowlist="emandate")
        created, demo, config = setup_subscription(session, provider, config=config)
        cycle = add_cycle(provider, demo)
        set_subscription(provider, demo, "halted", "emandate")
        case_id = process_subscription_payload(
            session,
            provider,
            config,
            mandate_failure_payload(demo, cycle),
            "evt_mandate_contract_failure",
            NOW + timedelta(minutes=1),
        )
        diagnose_case(session, case_id)
        token = issue_demo_recovery_token(
            session, demo.id, settings=config, now=NOW + timedelta(minutes=1)
        )
        get_recovery_bootstrap(
            session,
            token,
            provider=provider,
            settings=config,
            now=NOW + timedelta(minutes=1),
        )

        set_subscription(provider, demo, "active", "emandate")
        activated = {
            "event": "subscription.activated",
            "payload": {
                "subscription": {"entity": {"id": demo.primary_entity_id}},
                "invoice": {
                    "entity": {"id": cycle.id, "subscription_id": demo.primary_entity_id}
                },
            },
        }
        process_subscription_payload(
            session,
            provider,
            config,
            activated,
            "evt_mandate_contract_repair",
            NOW + timedelta(minutes=2),
        )
        capture_cycle(
            provider,
            cycle,
            payment_id="pay_mandate_contract",
            occurred=NOW + timedelta(minutes=3),
        )
        reconcile_subscription(
            session,
            demo.id,
            provider=provider,
            settings=config,
            now=NOW + timedelta(minutes=3),
        )
        artifact = build_demo_acceptance_export(
            session,
            demo.id,
            session_token=created.session_token,
            settings=config,
            now=NOW + timedelta(minutes=4),
        )
        checks = {item.check: item.passed for item in artifact.checks}
        assert artifact.passed, [(item.check, item.passed) for item in artifact.checks]
        assert artifact.session.scenario_type == "MANDATE_BROKEN"
        assert artifact.case.leak_type == "MANDATE_BROKEN"
        assert artifact.data_provenance == "SIMULATED_END_TO_END"
        assert checks["qualified_mandate_evidence"]
        assert checks["authorization_repair_separate_from_revenue"]
        assert checks["captured_payment_globally_unique"]
        output = request.config.getoption("--acceptance-output-dir")
        if output:
            from pathlib import Path

            Path(output).mkdir(parents=True, exist_ok=True)
            Path(output, "mandate-broken-contract.json").write_text(
                artifact.model_dump_json() + "\n"
            )


def test_mandate_repair_respects_customer_opt_out_and_intentional_cancellation(session_factory):
    provider = FakePaymentProvider()
    with session_factory() as session:
        _, demo, config = setup_subscription(session, provider)
        cycle = add_cycle(provider, demo)
        set_subscription(provider, demo, "halted", "emandate")
        case_id = process_subscription_payload(
            session,
            provider,
            config,
            mandate_failure_payload(demo, cycle),
            "evt_optout_mandate",
            NOW + timedelta(minutes=1),
        )
        customer = session.get(Customer, demo.customer_id)
        customer.dnc = True
        action = schedule_demo_recovery_email(session, case_id, settings=config, now=NOW)
        set_subscription(provider, demo, "cancelled", "emandate")
        reconcile_subscription(
            session, demo.id, provider=provider, settings=config, now=NOW + timedelta(minutes=2)
        )
        session.refresh(action)
        view = subscription_view(session, demo, NOW)
        assert action.status == "cancelled"
        assert view["method_update_available"] is False
        assert view["authorization_repaired"] is False
        assert session.scalar(select(func.count(Settlement.id))) == 0
