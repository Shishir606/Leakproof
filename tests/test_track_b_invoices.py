from __future__ import annotations

import json
from datetime import timedelta

import httpx2
import pytest
from sqlalchemy import func, select
from test_api_september_4 import NOW, settings

from leakproof.audit.timeline import replay_case
from leakproof.demo.acceptance import build_demo_acceptance_export
from leakproof.demo.contracts import DemoSessionCreateRequest
from leakproof.demo.email import execute_demo_recovery_email, schedule_demo_recovery_email
from leakproof.demo.invoices import (
    invoice_view,
    reconcile_invoice,
    reconcile_invoice_sessions,
    safe_invoice_url,
)
from leakproof.demo.projection import get_demo_session_projection
from leakproof.demo.rate_limit import InMemoryRateLimiter
from leakproof.demo.security import RecoveryTokenClaims, issue_resource_recovery_token
from leakproof.demo.service import (
    RecoveryExpired,
    RecoveryTokenInvalid,
    create_demo_session,
    get_recovery_bootstrap,
    issue_demo_recovery_token,
)
from leakproof.diagnosis import diagnose_case
from leakproof.models.db import (
    DemoSession,
    ProviderCall,
    ProviderObligation,
    RecoveryCase,
    Settlement,
)
from leakproof.models.resources import EntityRef
from leakproof.providers import Payment, ProviderError
from leakproof.providers.contracts import CreateInvoiceRequest
from leakproof.providers.fakes import FakeEmailProvider, FakePaymentProvider
from leakproof.providers.razorpay import RazorpayPaymentProvider
from leakproof.sensors.processor import process_stored_webhook
from leakproof.sensors.webhooks import persist_webhook


def setup_invoice(session, provider, config=None, recipient=None):
    config = config or settings()
    created = create_demo_session(
        session,
        DemoSessionCreateRequest(scenario_type="INVOICE_OVERDUE", recipient=recipient),
        client_ip="127.0.0.1",
        provider=provider,
        limiter=InMemoryRateLimiter(),
        settings=config,
        now=NOW,
    )
    return created, session.get(DemoSession, created.session_id), config


def reconcile(session, demo, provider, config, seconds=61):
    return reconcile_invoice(
        session, demo.id, provider=provider, settings=config, now=NOW + timedelta(seconds=seconds)
    )


def pay(provider, demo, paid, *, status=None, payment_id="pay_partial", amount=None, seconds=70):
    inv = provider.invoices[demo.primary_entity_id]
    provider.invoices[inv.id] = inv.model_copy(
        update={
            "amount_paid_paise": paid,
            "amount_due_paise": inv.amount_paise - paid,
            "status": status or ("paid" if paid == inv.amount_paise else "partially_paid"),
        }
    )
    provider.payments[payment_id] = Payment(
        payment_id,
        inv.order_id,
        amount or paid,
        inv.currency,
        "captured",
        created_at=int((NOW + timedelta(seconds=seconds)).timestamp()),
        invoice_id=inv.id,
    )


def webhook(session, demo, provider, config, event, *, seconds=90, merchant=None, event_id=None):
    # Deliberately stale event entity: only its explicit identity wakes the current API read.
    inv = provider.invoices[demo.primary_entity_id]
    payload = {
        "event": event,
        "created_at": int(NOW.timestamp()),
        "payload": {
            "invoice": {"entity": {"id": inv.id, "status": "expired", "amount_due": 50000}},
            "payment": {"entity": {"id": "pay_partial", "order_id": inv.order_id}},
        },
    }
    stored = persist_webhook(
        session,
        merchant_id=merchant or demo.merchant_id,
        payload=payload,
        header_event_id=event_id or f"evt_{event}_{seconds}",
    )
    result = process_stored_webhook(
        session, stored.id, provider=provider, settings=config, now=NOW + timedelta(seconds=seconds)
    )
    return result, stored


def test_invoice_contract_setup_notifications_partial_expiry_and_strict_decode():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(
            200,
            json={
                "id": "inv_contract",
                "status": "draft",
                "customer_id": "cust_test",
                "amount": 50000,
                "amount_paid": 0,
                "amount_due": 50000,
                "currency": "INR",
                "partial_payment": True,
                "issued_at": None,
                "expire_by": 1234567890,
                "short_url": None,
            },
        )

    adapter = RazorpayPaymentProvider(
        "rzp_test_contract",
        "secret",
        client=httpx2.Client(
            base_url="https://api.razorpay.com", transport=httpx2.MockTransport(handler)
        ),
    )
    result = adapter.create_invoice(
        CreateInvoiceRequest(
            amount_paise=50000,
            currency="INR",
            customer_id="cust_test",
            receipt="test",
            line_item_name="Test",
            idempotency_key="test",
            expire_by=1234567890,
        )
    )
    body = json.loads(seen[0].content)
    assert body["sms_notify"] is body["email_notify"] is False
    assert body["partial_payment"] is True and body["draft"] == "1"
    assert result.expire_by == body["expire_by"] == 1234567890
    with pytest.raises(ProviderError):
        adapter._decode_invoice({"id": "inv_bad", "status": "unknown"})


def test_adapter_normalizes_only_razorpay_draft_null_balances():
    adapter = RazorpayPaymentProvider("rzp_test_contract", "secret")
    draft = {
        "id": "inv_draft",
        "status": "draft",
        "customer_id": "cust_test",
        "amount": 50000,
        "amount_paid": None,
        "amount_due": None,
        "currency": "INR",
        "partial_payment": True,
    }

    decoded = adapter._decode_invoice(draft)
    assert decoded.amount_paid_paise == 0
    assert decoded.amount_due_paise == 50000

    with pytest.raises(ProviderError):
        adapter._decode_invoice({**draft, "status": "issued"})


def test_due_date_is_separate_from_provider_expiry_and_original_recovery(session_factory):
    provider = FakePaymentProvider()
    with session_factory() as session:
        created, demo, config = setup_invoice(session, provider)
        assert not provider.orders and len(provider.invoice_create_calls) == 1
        assert reconcile(session, demo, provider, config, 59) is None
        view = invoice_view(session, demo, NOW + timedelta(seconds=59))
        assert not view["business_overdue"] and view["aging_bucket"] == "not_due"
        case = reconcile(session, demo, provider, config)
        assert case.leak_type == "INVOICE_OVERDUE" and case.amount_at_risk == 50000
        token = issue_demo_recovery_token(
            session, demo.id, settings=config, now=NOW + timedelta(seconds=62)
        )
        bootstrap = get_recovery_bootstrap(
            session, token, provider=provider, settings=config, now=NOW + timedelta(seconds=63)
        )
        assert bootstrap.purpose == "invoice_hosted_payment"
        assert bootstrap.redirect_url == provider.invoices[demo.primary_entity_id].short_url
        assert bootstrap.amount_due_paise == 50000
        assert len(provider.invoice_create_calls) == len(provider.invoice_issue_calls) == 1
        assert invoice_view(session, demo, NOW + timedelta(seconds=63))["business_overdue"]
        assert session.scalar(select(func.count(RecoveryCase.id))) == 1


def test_partial_overlap_duplicate_out_of_order_and_sanitized_acceptance(session_factory, request):
    provider = FakePaymentProvider()
    with session_factory() as session:
        created, demo, config = setup_invoice(session, provider)
        case = reconcile(session, demo, provider, config)
        diagnose_case(session, case.id)
        action = schedule_demo_recovery_email(
            session, case.id, settings=config, now=NOW + timedelta(seconds=61)
        )
        token = issue_demo_recovery_token(
            session, demo.id, settings=config, now=NOW + timedelta(seconds=62)
        )
        get_recovery_bootstrap(
            session, token, provider=provider, settings=config, now=NOW + timedelta(seconds=63)
        )
        pay(provider, demo, 20000)
        for index, event in enumerate(
            ["invoice.partially_paid", "payment.captured", "order.paid", "subscription.charged"]
        ):
            result, _ = webhook(session, demo, provider, config, event, seconds=80 + index)
            assert result == case.id and case.outcome != "RECOVERED"
        # Inbox duplicate and distinct overlapping notifications are both idempotent.
        _, first = webhook(
            session, demo, provider, config, "invoice.partially_paid", event_id="evt_duplicate"
        )
        result, duplicate = webhook(
            session, demo, provider, config, "invoice.partially_paid", event_id="evt_duplicate"
        )
        assert duplicate.duplicate and result is None
        obligation = session.scalar(
            select(ProviderObligation).where(ProviderObligation.case_id == case.id)
        )
        assert obligation.detected_due_paise == case.amount_at_risk == 50000
        assert obligation.outstanding_paise == 30000 and obligation.recovered_paise == 20000
        assert session.scalar(select(func.count(Settlement.id))) == 1
        assert demo.state == "AT_RISK" and action.status == "pending"
        pay(provider, demo, 50000, payment_id="pay_remaining", amount=30000, seconds=100)
        for index, event in enumerate(
            ["invoice.paid", "invoice.expired", "invoice.partially_paid", "payment.captured"]
        ):
            result, _ = webhook(session, demo, provider, config, event, seconds=110 + index)
            assert result == case.id
        assert demo.state == "RECOVERED" and case.outcome == "RECOVERED"
        assert obligation.recovered_paise == 50000 and obligation.outstanding_paise == 0
        assert action.status == "cancelled"
        assert session.scalar(select(func.count(Settlement.id))) == 2
        assert session.scalar(select(func.count(RecoveryCase.id))) == 1
        assert replay_case(session, case.id).projection_matches
        export = build_demo_acceptance_export(
            session,
            demo.id,
            session_token=created.session_token,
            settings=config,
            now=NOW + timedelta(seconds=120),
        )
        assert export.passed, [(c.check, c.passed) for c in export.checks]
        raw = export.model_dump_json()
        for private in [
            demo.id,
            demo.primary_entity_id,
            created.session_token,
            "pay_partial",
            "https://rzp.io",
        ]:
            assert private not in raw
        assert export.data_provenance == "SIMULATED_END_TO_END"
        output = request.config.getoption("--acceptance-output-dir")
        if output:
            from pathlib import Path

            Path(output).mkdir(parents=True, exist_ok=True)
            Path(output, "invoice-partial-full.json").write_text(raw + "\n")


@pytest.mark.parametrize("state", ["expired", "cancelled", "deleted", "draft"])
def test_nonpayable_invoices_have_no_cta_or_replacement(session_factory, state):
    provider = FakePaymentProvider()
    with session_factory() as session:
        created, demo, config = setup_invoice(session, provider)
        case = reconcile(session, demo, provider, config)
        action = schedule_demo_recovery_email(
            session, case.id, settings=config, now=NOW + timedelta(seconds=61)
        )
        token = issue_demo_recovery_token(
            session, demo.id, settings=config, now=NOW + timedelta(seconds=62)
        )
        inv = provider.invoices[demo.primary_entity_id]
        provider.invoices[inv.id] = inv.model_copy(update={"status": state})
        bootstrap = get_recovery_bootstrap(
            session, token, provider=provider, settings=config, now=NOW + timedelta(seconds=65)
        )
        assert bootstrap.redirect_url is None and bootstrap.disposition == "merchant_review"
        assert case.outcome != "RECOVERED" and action.status == "cancelled"
        projection = get_demo_session_projection(
            session,
            demo.id,
            session_token=created.session_token,
            settings=config,
            now=NOW + timedelta(seconds=66),
        )
        assert not projection.recovery_url_available
        assert len(provider.invoice_create_calls) == len(provider.invoice_issue_calls) == 1


def test_expiry_timestamp_blocks_issued_invoice_without_claiming_paid(session_factory):
    provider = FakePaymentProvider()
    with session_factory() as session:
        _, demo, config = setup_invoice(session, provider)
        inv = provider.invoices[demo.primary_entity_id]
        provider.invoices[inv.id] = inv.model_copy(
            update={"expire_by": int((NOW + timedelta(seconds=30)).timestamp())}
        )
        case = reconcile(session, demo, provider, config, 31)
        assert case is None  # Expiry before app aging is not overdue.
        assert (
            invoice_view(session, demo, NOW + timedelta(seconds=31))["disposition"]
            == "merchant_review"
        )
        assert demo.state != "RECOVERED"


@pytest.mark.parametrize(
    "url",
    [
        "http://rzp.io/i/a",
        "https://rzp.io.evil.test/i/a",
        "https://user@rzp.io/i/a",
        "https://rzp.io:443/i/a",
        "https://rzp.io/i/a?next=x",
        "https://rzp.io/i/a#x",
        "https://evil.test/i/a",
        "https://rzp.io/i/%2f%2fevil.test",
        "https://rzp.io/other/a",
        "https://rzp.io\\@evil.test/i/a",
        None,
    ],
)
def test_hosted_url_allowlist(url):
    assert not safe_invoice_url(url)


@pytest.mark.parametrize(
    "url",
    ["https://rzp.io/i/fixture_1", "https://rzp.io/rzp/fixture-2"],
)
def test_hosted_url_allowlist_accepts_provider_invoice_forms(url):
    assert safe_invoice_url(url)


def test_wrong_merchant_and_wrong_provider_entities_fail_closed(session_factory):
    provider = FakePaymentProvider()
    with session_factory() as session:
        _, demo, config = setup_invoice(session, provider)
        case = reconcile(session, demo, provider, config)
        result, _ = webhook(session, demo, provider, config, "invoice.paid", merchant="other")
        assert result is None and case.outcome != "RECOVERED"
        assert session.scalar(select(func.count(RecoveryCase.id))) == 1
        original = provider.invoices[demo.primary_entity_id]
        for changes in [
            {"id": "inv_other"},
            {"customer_id": "cust_other"},
            {"currency": "USD"},
            {"order_id": "order_other"},
        ]:
            provider.invoices[original.id] = original.model_copy(update=changes)
            with pytest.raises(ProviderError, match="ownership"):
                reconcile(session, demo, provider, config, 100)
            assert (
                invoice_view(session, demo, NOW + timedelta(seconds=100))["disposition"]
                == "provider_retry"
            )
        provider.invoices[original.id] = original
        provider.payments["pay_wrong"] = Payment(
            "pay_wrong", original.order_id, 10, "USD", "captured"
        )
        with pytest.raises(ProviderError):
            reconcile(session, demo, provider, config, 101)
        assert session.scalar(select(func.count(Settlement.id))) == 0


def test_provider_failure_retryable_inbox_and_reconciliation_job(session_factory):
    provider = FakePaymentProvider()
    with session_factory() as session:
        _, demo, config = setup_invoice(session, provider)
        case = reconcile(session, demo, provider, config)
        provider.failure = ProviderError(
            "razorpay", "fetch_invoice", "timeout", True, "Provider unavailable"
        )
        with pytest.raises(ProviderError):
            webhook(session, demo, provider, config, "invoice.paid", event_id="evt_retry")
        assert case.outcome != "RECOVERED"
        failed = session.scalars(select(ProviderCall).where(ProviderCall.status == "failed")).all()
        assert len(failed) == 1
        provider.failure = None
        result, _ = webhook(
            session, demo, provider, config, "invoice.paid", event_id="evt_retry", seconds=110
        )
        assert result == case.id and demo.state == "AT_RISK"
    stats = reconcile_invoice_sessions(
        session_factory=session_factory,
        provider=provider,
        settings=config,
        now=NOW + timedelta(seconds=145),
    )
    assert stats["scanned"] == 1 and stats["failed"] == 0


def test_baseline_partials_and_authorization_created_before_detection(session_factory):
    provider = FakePaymentProvider()
    with session_factory() as session:
        _, demo, config = setup_invoice(session, provider)
        pay(provider, demo, 10000, payment_id="pay_baseline", seconds=20)
        assert reconcile(session, demo, provider, config, 30) is None
        case = reconcile(session, demo, provider, config)
        assert case.amount_at_risk == 40000
        # Created earlier, captured only after detection: its newly verified amount earns credit.
        pay(provider, demo, 50000, payment_id="pay_final", amount=40000, seconds=40)
        reconcile(session, demo, provider, config, 80)
        ledger = list(session.scalars(select(Settlement).order_by(Settlement.id)))
        assert [p.credited_paise for p in ledger] == [0, 40000]
        assert case.outcome == "RECOVERED" and case.amount_at_risk == 40000


def test_paid_first_never_creates_retrospective_case(session_factory):
    provider = FakePaymentProvider()
    with session_factory() as session:
        _, demo, config = setup_invoice(session, provider)
        pay(provider, demo, 50000, seconds=10)
        assert reconcile(session, demo, provider, config, 20) is None
        result, _ = webhook(session, demo, provider, config, "invoice.expired")
        assert result is None and demo.state == "RECOVERED"
        assert session.scalar(select(func.count(RecoveryCase.id))) == 0
        assert session.scalar(select(func.sum(Settlement.credited_paise))) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"merchant_id": "other"},
        {"amount_paise": 1},
        {"currency": "USD"},
        {"session_id": "demo_missing"},
        {"entity": EntityRef(entity_type="invoice", entity_id="inv_other")},
    ],
)
def test_invoice_tokens_bind_all_claims(session_factory, change):
    provider = FakePaymentProvider()
    with session_factory() as session:
        _, demo, config = setup_invoice(session, provider)
        reconcile(session, demo, provider, config)
        claims = dict(
            version=2,
            session_id=demo.id,
            merchant_id=demo.merchant_id,
            scenario_type="INVOICE_OVERDUE",
            purpose="invoice_hosted_payment",
            entity=EntityRef(entity_type="invoice", entity_id=demo.primary_entity_id),
            amount_paise=demo.amount_paise,
            currency=demo.currency,
            expires_at=NOW + timedelta(minutes=20),
        )
        token = issue_resource_recovery_token(
            RecoveryTokenClaims(**(claims | change)), config.recovery_token_secret
        )
        with pytest.raises(RecoveryTokenInvalid):
            get_recovery_bootstrap(
                session, token, provider=provider, settings=config, now=NOW + timedelta(seconds=62)
            )
        valid = issue_demo_recovery_token(
            session, demo.id, settings=config, now=NOW + timedelta(seconds=62)
        )
        with pytest.raises(RecoveryTokenInvalid):
            get_recovery_bootstrap(
                session,
                valid + "tampered",
                provider=provider,
                settings=config,
                now=NOW + timedelta(seconds=62),
            )
        with pytest.raises(RecoveryExpired):
            get_recovery_bootstrap(
                session, valid, provider=provider, settings=config, now=NOW + timedelta(hours=2)
            )


def test_issue_failure_preserves_original_draft_and_optional_email_is_safe(session_factory):
    class IssueFailure(FakePaymentProvider):
        def issue_invoice(self, invoice_id):
            raise ProviderError("razorpay", "issue_invoice", "timeout", True, "Unavailable")

    provider = IssueFailure()
    with session_factory() as session:
        created, demo, config = setup_invoice(session, provider)
        assert created.setup_state == "ACTION_REQUIRED"
        assert demo.primary_entity_id in provider.invoices
        assert reconcile(session, demo, provider, config) is None
        assert len(provider.invoice_create_calls) == 1


def test_optional_email_uses_outstanding_invoice_wording_and_no_unallowlisted_send(session_factory):
    provider = FakePaymentProvider()
    mail = FakeEmailProvider()
    with session_factory() as session:
        _, demo, config = setup_invoice(session, provider, recipient="reviewer@example.com")
        case = reconcile(session, demo, provider, config)
        diagnose_case(session, case.id)
        action = schedule_demo_recovery_email(
            session, case.id, settings=config, now=NOW + timedelta(seconds=61)
        )
        pay(provider, demo, 20000)
        result = execute_demo_recovery_email(
            session,
            action.id,
            provider=mail,
            invoice_provider=provider,
            settings=config,
            now=NOW + timedelta(seconds=100),
        )
        assert result.status == "sent"
        assert "INR 300.00" in mail.calls[0].template_variables["body"]
        assert mail.calls[0].template_id == "util_invoice_recovery_email_v1"
        assert "Checkout" not in mail.calls[0].template_variables["body"]
        execute_demo_recovery_email(
            session,
            action.id,
            provider=mail,
            invoice_provider=provider,
            settings=config,
            now=NOW + timedelta(seconds=101),
        )
        assert len(mail.calls) == 1


@pytest.mark.parametrize("state", ["expired", "cancelled"])
def test_nonpayable_capture_is_sanitized_and_complete(session_factory, request, state):
    provider = FakePaymentProvider()
    with session_factory() as session:
        created, demo, config = setup_invoice(session, provider)
        inv = provider.invoices[demo.primary_entity_id]
        provider.invoices[inv.id] = inv.model_copy(update={"status": state})
        reconcile(session, demo, provider, config)
        export = build_demo_acceptance_export(
            session,
            demo.id,
            session_token=created.session_token,
            settings=config,
            now=NOW + timedelta(seconds=65),
        )
        assert export.passed, [(c.check, c.passed) for c in export.checks]
        assert export.invoice.disposition == "merchant_review"
        output = request.config.getoption("--acceptance-output-dir")
        if output:
            from pathlib import Path

            Path(output).mkdir(parents=True, exist_ok=True)
            Path(output, f"invoice-{state}.json").write_text(export.model_dump_json() + "\n")


def test_contradictory_snapshot_or_missing_payment_ids_hold_cta_without_money(session_factory):
    provider = FakePaymentProvider()
    with session_factory() as session:
        _, demo, config = setup_invoice(session, provider)
        case = reconcile(session, demo, provider, config)
        original = provider.invoices[demo.primary_entity_id]
        provider.invoices[original.id] = original.model_copy(
            update={"status": "paid", "amount_paid_paise": 50000, "amount_due_paise": 0}
        )
        with pytest.raises(ProviderError, match="reconciliation"):
            reconcile(session, demo, provider, config, 80)
        assert case.outcome != "RECOVERED" and not list(session.scalars(select(Settlement)))
        pay(provider, demo, 50000, seconds=70)
        reconcile(session, demo, provider, config, 90)
        assert case.outcome == "RECOVERED"
        provider.invoices[original.id] = original.model_copy(update={"status": "expired"})
        with pytest.raises(ProviderError):
            reconcile(session, demo, provider, config, 100)
        assert case.outcome == "RECOVERED"
        obligation = session.scalar(
            select(ProviderObligation).where(ProviderObligation.case_id == case.id)
        )
        assert obligation.outstanding_paise == 0 and obligation.recovered_paise == 50000


def test_bare_order_and_payment_events_resolve_registered_invoice(session_factory):
    provider = FakePaymentProvider()
    with session_factory() as session:
        _, demo, config = setup_invoice(session, provider)
        case = reconcile(session, demo, provider, config)
        pay(provider, demo, 20000)
        inv = provider.invoices[demo.primary_entity_id]
        for event, payload in [
            (
                "payment.captured",
                {
                    "payment": {
                        "entity": {
                            "id": "pay_partial",
                            "order_id": inv.order_id,
                            "amount": 20000,
                            "currency": "INR",
                        }
                    }
                },
            ),
            (
                "order.paid",
                {"order": {"entity": {"id": inv.order_id, "amount": 50000, "currency": "INR"}}},
            ),
        ]:
            stored = persist_webhook(
                session,
                merchant_id=demo.merchant_id,
                payload={"event": event, "created_at": int(NOW.timestamp()), "payload": payload},
                header_event_id=event,
            )
            assert (
                process_stored_webhook(
                    session,
                    stored.id,
                    provider=provider,
                    settings=config,
                    now=NOW + timedelta(seconds=80),
                )
                == case.id
            )
        assert case.outcome != "RECOVERED" and demo.state == "AT_RISK"
        assert session.scalar(select(func.count(Settlement.id))) == 1


def test_unallowlisted_invoice_recipient_stays_preview_only(session_factory):
    provider, mail = FakePaymentProvider(), FakeEmailProvider()
    with session_factory() as session:
        _, demo, config = setup_invoice(session, provider, recipient="outside@example.com")
        case = reconcile(session, demo, provider, config)
        diagnose_case(session, case.id)
        action = schedule_demo_recovery_email(
            session, case.id, settings=config, now=NOW + timedelta(seconds=61)
        )
        result = execute_demo_recovery_email(
            session,
            action.id,
            provider=mail,
            invoice_provider=provider,
            settings=config,
            now=NOW + timedelta(seconds=100),
        )
        assert result.status == "preview_only" and not mail.calls
        assert demo.recipient_ciphertext is None


def test_invoice_http_route_and_signed_webhook_contract(
    client, session_factory, payment_provider, monkeypatch
):
    from datetime import UTC, datetime

    from test_webhooks import signed_body

    from leakproof.demo.invoices import invoice_entity

    config = settings()
    monkeypatch.setattr("leakproof.api.app.get_settings", lambda: config)
    response = client.post("/demo/sessions", json={"scenario_type": "INVOICE_OVERDUE"})
    assert response.status_code == 201
    created = response.json()
    assert created["primary_entity_type"] == "invoice" and "razorpay_order_id" not in created
    now = datetime.now(UTC)
    with session_factory() as session:
        demo = session.get(DemoSession, created["session_id"])
        entity = invoice_entity(session, demo)
        entity.safe_metadata = {
            **entity.safe_metadata,
            "business_due_at": (now - timedelta(seconds=1)).isoformat(),
        }
        session.commit()
        case = reconcile_invoice(
            session, demo.id, provider=payment_provider, settings=config, now=now
        )
        token = issue_demo_recovery_token(session, demo.id, settings=config, now=now)
        case_id = case.id
    boot = client.get(f"/recover/{token}")
    assert boot.status_code == 200 and boot.json()["purpose"] == "invoice_hosted_payment"
    invalid = client.get(f"/recover/{token}tamper")
    assert invalid.status_code == 404
    body, signature = signed_body(
        {
            "event": "invoice.partially_paid",
            "created_at": int(now.timestamp()),
            "payload": {"invoice": {"entity": {"id": created["primary_entity_id"]}}},
        }
    )
    for expected in [False, True]:
        result = client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "content-type": "application/json",
                "x-razorpay-signature": signature,
                "x-razorpay-event-id": "evt_invoice_http",
                "x-leakproof-merchant-id": config.default_merchant_id,
            },
        )
        assert result.status_code == 200
        assert result.json()["duplicate"] == expected
    with session_factory() as session:
        assert session.get(RecoveryCase, case_id).outcome != "RECOVERED"


def test_invoice_aging_promotes_existing_lower_priority_case_without_resetting_balance(
    session_factory,
):
    from leakproof.models.resources import ObligationRef, ProviderScope, RiskSignal
    from leakproof.provider_resources import record_risk

    provider = FakePaymentProvider()
    with session_factory() as session:
        _, demo, config = setup_invoice(session, provider)
        ref = ObligationRef(entity_type="invoice", entity_id=demo.primary_entity_id)
        case, _ = record_risk(
            session,
            RiskSignal(
                scope=ProviderScope(merchant_id=demo.merchant_id),
                entity=ref,
                obligation=ref,
                source="razorpay_api",
                occurred_at=NOW + timedelta(seconds=20),
                leak_type="PAYMENT_FAILURE",
                customer_id=demo.customer_id,
                amount_due_paise=50000,
                currency="INR",
            ),
        )
        session.commit()
        same = reconcile(session, demo, provider, config)
        assert same.id == case.id and case.leak_type == "INVOICE_OVERDUE"
        assert case.amount_at_risk == 50000
        assert session.scalar(select(func.count(RecoveryCase.id))) == 1


def test_quarantined_obligation_cannot_claim_invoice_recovery(session_factory):
    provider = FakePaymentProvider()
    with session_factory() as session:
        _, demo, config = setup_invoice(session, provider)
        case = reconcile(session, demo, provider, config)
        obligation = session.scalar(
            select(ProviderObligation).where(ProviderObligation.case_id == case.id)
        )
        obligation.reconciliation_required = True
        session.commit()
        pay(provider, demo, 50000)
        with pytest.raises(ProviderError, match="merchant review"):
            reconcile(session, demo, provider, config, 80)
        assert case.outcome != "RECOVERED" and demo.state != "RECOVERED"
        assert obligation.recovered_paise == 0 and obligation.reconciliation_required
