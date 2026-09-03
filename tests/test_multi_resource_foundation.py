from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from test_api_august_31 import NOW, create_session, process_payload, razorpay_payload

from leakproof.audit.timeline import replay_case
from leakproof.demo.contracts import DemoSessionCreateRequest, ResourceRecoveryBootstrap
from leakproof.demo.projection import get_demo_session_projection
from leakproof.demo.security import (
    InvalidRecoveryToken,
    RecoveryTokenClaims,
    RecoveryTokenExpired,
    issue_recovery_token,
    issue_resource_recovery_token,
    issue_session_token,
    verify_recovery_token,
)
from leakproof.demo.service import RecoveryTokenInvalid, get_recovery_bootstrap
from leakproof.models.db import (
    Action,
    DemoSession,
    ProviderEntity,
    RecoveryAttribution,
    RecoveryCase,
    Settlement,
)
from leakproof.models.domain import LeakType
from leakproof.models.resources import (
    EntityRef,
    EntityStateSignal,
    EntityType,
    ObligationRef,
    ProviderScope,
    ProviderSignal,
    RecoveryPurpose,
    RecoverySignal,
    RiskSignal,
    SetupState,
)
from leakproof.provenance import DataProvenance
from leakproof.provider_resources import (
    get_obligation,
    record_recovery,
    record_risk,
    record_state,
    register_entity,
)
from leakproof.providers.contracts import Invoice, Subscription
from leakproof.sensors.normalizer import (
    normalize_razorpay,
    normalize_razorpay_paid,
    normalize_razorpay_state,
)

SCOPE = ProviderScope(merchant_id="merchant_resources")
INVOICE = ObligationRef(entity_type="invoice", entity_id="inv_cycle_1")
SUBSCRIPTION = EntityRef(entity_type="subscription", entity_id="sub_parent")


def risk(ref=INVOICE, **overrides):
    return RiskSignal(
        **{
            "scope": SCOPE,
            "entity": ref,
            "root": SUBSCRIPTION,
            "obligation": ref,
            "source": "razorpay_api",
            "occurred_at": NOW,
            "customer_id": "customer_resources",
            "leak_type": LeakType.INVOICE_OVERDUE,
            "amount_due_paise": 80,
            "baseline_paid_paise": 20,
            "currency": "INR",
            **overrides,
        }
    )


def payment(payment_id="pay_first", amount=30, due=50, ref=INVOICE, **overrides):
    return RecoverySignal(
        **{
            "scope": SCOPE,
            "entity": EntityRef(entity_type="payment", entity_id=payment_id),
            "root": ref,
            "obligation": ref,
            "source": "razorpay_webhook",
            "occurred_at": NOW + timedelta(minutes=1),
            "payment_id": payment_id,
            "amount_paise": amount,
            "amount_due_paise": due,
            "currency": "INR",
            "settlement": "captured_payment",
            **overrides,
        }
    )


def pending(session, case, suffix="one"):
    action = Action(
        id=f"action_{suffix}",
        case_id=case.id,
        step_index=0,
        action_type="email_link",
        scheduled_for=NOW,
        idempotency_key=f"idem_{suffix}",
        status="pending",
    )
    session.add(action)
    session.flush()
    return action


def test_partial_settlement_shared_ledger_caps_credit_and_replays(session_factory):
    with session_factory() as session:
        case, _ = record_risk(session, risk())
        action = pending(session, case)
        record_recovery(session, payment())
        record_recovery(session, payment())
        assert session.get(RecoveryCase, case.id).outcome is None
        assert action.status == "pending"
        full_without_payment = RecoverySignal(
            scope=SCOPE,
            entity=INVOICE,
            obligation=INVOICE,
            source="razorpay_webhook",
            occurred_at=NOW + timedelta(minutes=2),
            currency="INR",
            settlement="full_settlement",
            amount_due_paise=0,
        )
        record_recovery(session, full_without_payment)
        assert action.status == "cancelled"
        # Full/cumulative event stops contact but cannot guess the remaining payment credit.
        assert session.scalar(select(RecoveryAttribution.amount_paise)) == 30
        record_recovery(session, payment("pay_second", 50, 0))
        record_recovery(session, full_without_payment)
        record_recovery(session, payment("pay_second", 50, 0))
        assert session.scalar(select(RecoveryAttribution.amount_paise)) == 80
        assert session.scalar(select(func.sum(Settlement.credited_paise))) == 80
        assert session.scalar(select(func.count()).select_from(Settlement)) == 2
        assert replay_case(session, case.id).projection_matches
        # Extra payment evidence may be recorded, but cannot exceed the frozen unpaid balance.
        record_recovery(session, payment("pay_extra", 100, 0))
        assert session.scalar(select(RecoveryAttribution.amount_paise)) == 80


def test_same_obligation_precedence_preserves_owner_arm_window_and_credit(session_factory):
    with session_factory() as session:
        case, _ = record_risk(session, risk(leak_type=LeakType.PAYMENT_FAILURE))
        original = case.id, case.arm, case.detected_at, case.attribution_until
        action = pending(session, case)
        record_recovery(session, payment())
        promoted, created = record_risk(
            session, risk(entity=SUBSCRIPTION, root=None, leak_type=LeakType.SUBSCRIPTION_HALT)
        )
        assert not created and promoted.id == case.id
        assert (case.id, case.arm, case.detected_at, case.attribution_until) == original
        assert action.status == "cancelled"
        assert case.leak_type == LeakType.SUBSCRIPTION_HALT
        record_risk(session, risk(leak_type=LeakType.PAYMENT_FAILURE))
        assert case.leak_type == LeakType.SUBSCRIPTION_HALT
        assert session.scalar(select(RecoveryAttribution.amount_paise)) == 30
        assert replay_case(session, case.id).projection_matches


def test_two_unpaid_cycles_and_authorization_repair_do_not_close_each_other(session_factory):
    second = ObligationRef(entity_type="invoice", entity_id="inv_cycle_2")
    with session_factory() as session:
        first_case, _ = record_risk(session, risk())
        second_case, _ = record_risk(session, risk(second))
        for state in ("active", "authorization_repaired"):
            record_state(
                session,
                EntityStateSignal(
                    scope=SCOPE,
                    entity=SUBSCRIPTION,
                    source="razorpay_webhook",
                    occurred_at=NOW,
                    state=state,
                ),
            )
        assert first_case.outcome is None and second_case.outcome is None
        record_recovery(session, payment(ref=second, amount=80, due=0))
        assert second_case.outcome == "RECOVERED" and first_case.outcome is None
        assert (
            session.scalar(
                select(ProviderEntity).where(
                    ProviderEntity.provider_entity_id == SUBSCRIPTION.entity_id
                )
            ).obligation_id
            is None
        )
        with pytest.raises(ValueError, match="conflict"):
            record_recovery(session, payment(amount=80, due=0))


def test_success_first_and_pre_detection_payment_never_get_retrospective_credit(session_factory):
    with session_factory() as session:
        record_recovery(session, payment(amount=80, due=0))
        assert record_risk(session, risk()) == (None, False)
        assert session.scalar(select(func.count()).select_from(RecoveryAttribution)) == 0
        assert session.scalar(select(func.count()).select_from(RecoveryCase)) == 0
    with session_factory() as session:
        second = ObligationRef(entity_type="invoice", entity_id="inv_unpaid")
        case, _ = record_risk(session, risk(second))
        record_recovery(
            session, payment(ref=second, payment_id="pay_old", occurred_at=NOW - timedelta(days=1))
        )
        assert session.scalar(select(func.sum(Settlement.credited_paise))) == 0
        assert case.outcome is None


def test_missing_cycle_is_provisional_and_delayed_subscription_state_uses_event_time(
    session_factory,
):
    payload = {
        "event": "subscription.halted",
        "created_at": int(NOW.timestamp()),
        "payload": {
            "subscription": {
                "entity": {
                    "id": "sub_parent",
                    "paid_count": 9,
                    "amount": 1000,
                    "created_at": int((NOW - timedelta(days=90)).timestamp()),
                }
            }
        },
    }
    signal = normalize_razorpay_state(SCOPE.merchant_id, payload)
    assert signal.occurred_at == NOW and signal.obligation is None
    assert normalize_razorpay(SCOPE.merchant_id, payload) is None
    with session_factory() as session:
        assert record_risk(session, risk(entity=SUBSCRIPTION, obligation=None)) == (None, False)
        record_state(session, signal.model_copy(update={"state": "active"}))
        record_state(session, signal.model_copy(update={"occurred_at": NOW - timedelta(days=1)}))
        assert session.scalar(select(ProviderEntity.status)) == "active"
        assert session.scalar(select(func.count()).select_from(RecoveryCase)) == 0


def test_explicit_recurring_payments_cannot_use_legacy_customer_amount_fallback():
    for event in ("payment.failed", "payment.captured"):
        payload = razorpay_payload(event, "order_cycle")
        payload["payload"]["payment"]["entity"]["invoice_id"] = "inv_cycle"
        assert normalize_razorpay("merchant", payload) is None
        assert normalize_razorpay_paid("merchant", payload) is None


def test_late_invoice_relationship_moves_case_and_ledger_atomically(session_factory):
    order = ObligationRef(entity_type="order", entity_id="order_original")
    with session_factory() as session:
        case, _ = record_risk(session, risk(order, root=None))
        record_recovery(session, payment(ref=order))
        canonical = get_obligation(session, SCOPE, INVOICE, "INR")
        register_entity(session, SCOPE, order, root=INVOICE, obligation=canonical)
        same, created = record_risk(session, risk())
        assert same.id == case.id and not created
        assert session.scalar(select(func.count()).select_from(RecoveryCase)) == 1
        assert session.scalar(select(Settlement.obligation_id)) == canonical.id
        assert canonical.recovered_paise == 30
        assert replay_case(session, case.id).projection_matches


def test_conflicting_legacy_owners_quarantine_and_cancel_contact(session_factory):
    order = ObligationRef(entity_type="order", entity_id="order_legacy")
    with session_factory() as session:
        a, _ = record_risk(session, risk(order, root=None))
        b, _ = record_risk(session, risk())
        actions = [pending(session, a, "a"), pending(session, b, "b")]
        canonical = get_obligation(session, SCOPE, INVOICE, "INR")
        register_entity(session, SCOPE, order, root=INVOICE, obligation=canonical)
        assert canonical.reconciliation_required
        assert all(action.status == "cancelled" for action in actions)
        assert record_recovery(session, payment()) is None
        assert record_risk(session, risk()) == (None, False)
        assert session.scalar(select(func.count()).select_from(RecoveryAttribution)) == 0


def test_merchant_mode_isolation_and_session_foreign_keys(session_factory):
    with session_factory() as session:
        session.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
        created, _, _ = create_session(session)
        demo = session.get(DemoSession, created.session_id)
        other_scope = ProviderScope(merchant_id="merchant_other")
        ref = ObligationRef(entity_type="order", entity_id=demo.primary_entity_id)
        other = get_obligation(session, other_scope, ref, "INR")
        with pytest.raises(ValueError, match="session scope"):
            register_entity(session, other_scope, ref, session_id=demo.id, obligation=other)
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(
                ProviderEntity(
                    merchant_id=other_scope.merchant_id,
                    session_id=demo.id,
                    provider="razorpay",
                    mode="test",
                    entity_type="order",
                    provider_entity_id="order_injected",
                    role="primary",
                )
            )
            session.flush()
        live = ProviderScope(merchant_id=other_scope.merchant_id, mode="live")
        assert get_obligation(session, live, ref, "INR").id != other.id
        case, _ = record_risk(session, risk())
        with pytest.raises(ValueError, match="another merchant"):
            record_risk(session, risk(scope=other_scope))
        assert case.merchant_id == SCOPE.merchant_id


@pytest.mark.parametrize("scenario", list(LeakType))
def test_scenario_selection_is_exhaustive_but_unimplemented_routes_are_disabled(
    client, session_factory, payment_provider, scenario
):
    assert DemoSessionCreateRequest().scenario_type == LeakType.PAYMENT_FAILURE
    response = client.post("/demo/sessions", json={"scenario_type": scenario.value})
    enabled = scenario in {
        LeakType.PAYMENT_FAILURE, LeakType.CHECKOUT_ABANDON, LeakType.INVOICE_OVERDUE
    }
    assert response.status_code == (201 if enabled else 409)
    capabilities = client.get("/demo/scenarios").json()
    assert {item["scenario_type"] for item in capabilities} == {item.value for item in LeakType}
    if enabled:
        demo = response.json()
        assert demo["scenario_type"] == scenario and demo["setup_state"] == "READY"
    else:
        assert not payment_provider.orders
        assert response.json()["error"]["code"] == "scenario_not_implemented"


def test_selected_abandonment_can_detect_payment_failure_without_changing_setup(session_factory):
    from test_api_august_31 import settings

    from leakproof.demo.rate_limit import InMemoryRateLimiter
    from leakproof.demo.service import create_demo_session
    from leakproof.providers.fakes import FakePaymentProvider

    with session_factory() as session:
        config = settings()
        created = create_demo_session(
            session,
            DemoSessionCreateRequest(scenario_type="CHECKOUT_ABANDON"),
            client_ip="127.0.0.1",
            provider=FakePaymentProvider(),
            limiter=InMemoryRateLimiter(),
            settings=config,
            now=NOW,
        )
        process_payload(
            session,
            config.default_merchant_id,
            razorpay_payload("payment.failed", created.razorpay_order_id),
            "evt_actual_failure",
        )
        projection = get_demo_session_projection(
            session,
            created.session_id,
            session_token=created.session_token,
            settings=config,
            now=NOW,
        )
        assert projection.scenario_type == "CHECKOUT_ABANDON"
        assert projection.case.leak_type == "PAYMENT_FAILURE"
        assert projection.setup_state == "READY" and projection.state == "AT_RISK"


def _legacy_token(created, merchant, secret):
    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    payload = encode(
        json.dumps(
            {
                "purpose": "checkout_recovery",
                "sid": created.session_id,
                "mid": merchant,
                "oid": created.razorpay_order_id,
                "amount": created.amount_paise,
                "currency": created.currency,
                "exp": int((NOW + timedelta(minutes=5)).timestamp()),
            }
        ).encode()
    )
    signature = hmac.new(
        secret.encode(), f"recovery-token-v1.{payload}".encode(), hashlib.sha256
    ).digest()
    return f"{payload}.{encode(signature)}"


def test_v1_compatibility_v2_binding_and_wrong_purpose_rejected_before_provider(session_factory):
    with session_factory() as session:
        created, provider, config = create_session(session)
        demo = session.get(DemoSession, created.session_id)
        demo.state = "AT_RISK"
        legacy = _legacy_token(created, demo.merchant_id, config.recovery_token_secret)
        assert verify_recovery_token(legacy, config.recovery_token_secret, now=NOW).version == 1
        assert (
            get_recovery_bootstrap(
                session, legacy, provider=provider, settings=config, now=NOW
            ).razorpay_order_id
            == created.razorpay_order_id
        )
        claims = RecoveryTokenClaims(
            version=2,
            session_id=demo.id,
            merchant_id=demo.merchant_id,
            scenario_type="INVOICE_OVERDUE",
            entity=INVOICE,
            purpose="invoice_hosted_payment",
            amount_paise=demo.amount_paise,
            currency="INR",
            expires_at=NOW + timedelta(minutes=5),
        )
        token = issue_resource_recovery_token(claims, config.recovery_token_secret)
        assert (
            verify_recovery_token(token, config.recovery_token_secret, now=NOW).model_dump()
            == claims.model_dump()
        )
        with pytest.raises(RecoveryTokenInvalid):
            get_recovery_bootstrap(session, token, provider=provider, settings=config, now=NOW)
        with pytest.raises(InvalidRecoveryToken):
            verify_recovery_token(
                token,
                config.recovery_token_secret,
                now=NOW,
                expected_purpose=RecoveryPurpose.ORDER_CHECKOUT,
            )
        with pytest.raises(RecoveryTokenExpired):
            verify_recovery_token(token, config.recovery_token_secret, now=claims.expires_at)
        with pytest.raises(InvalidRecoveryToken):
            verify_recovery_token(
                token.replace("v2.", "v3."), config.recovery_token_secret, now=NOW
            )
        with pytest.raises(InvalidRecoveryToken):
            verify_recovery_token(
                issue_session_token(
                    demo.id, demo.merchant_id, claims.expires_at, config.recovery_token_secret
                ),
                config.recovery_token_secret,
                now=NOW,
            )
        wrong_scenario = issue_recovery_token(
            demo.id,
            demo.merchant_id,
            demo.razorpay_order_id,
            demo.amount_paise,
            "INR",
            claims.expires_at,
            config.recovery_token_secret,
            scenario_type=LeakType.CHECKOUT_ABANDON,
        )
        with pytest.raises(RecoveryTokenInvalid):
            get_recovery_bootstrap(
                session, wrong_scenario, provider=provider, settings=config, now=NOW
            )


@pytest.mark.parametrize(
    "field,value",
    [
        ("entity", {"entity_type": "invoice", "entity_id": "order_wrong"}),
        ("obligation", {"entity_type": "subscription", "entity_id": "sub_parent"}),
        ("leak_type", "NOT_IMPLEMENTED"),
        ("occurred_at", NOW.replace(tzinfo=None)),
        ("amount_due_paise", -1),
        ("leak_type", "MANDATE_BROKEN"),
    ],
)
def test_signal_contract_rejects_invalid_or_ambiguous_identity(field, value):
    with pytest.raises(ValidationError):
        risk(**{field: value})


def test_python_typescript_contracts_stay_exhaustive():
    source = Path("dashboard/lib/resource-types.ts").read_text()
    for name, enum in [
        ("LEAK_TYPES", LeakType),
        ("ENTITY_TYPES", EntityType),
        ("SETUP_STATES", SetupState),
        ("RECOVERY_PURPOSES", RecoveryPurpose),
        ("DATA_PROVENANCE", DataProvenance),
    ]:
        values = json.loads(re.search(rf"const {name} = (\[.*?\]) as const", source).group(1))
        assert values == [item.value for item in enum]
    adapter = TypeAdapter(ProviderSignal)
    assert adapter.validate_json(risk().model_dump_json()).model_dump() == risk().model_dump()
    for signal in (
        payment(),
        EntityStateSignal(
            scope=SCOPE,
            entity=SUBSCRIPTION,
            source="razorpay_webhook",
            occurred_at=NOW,
            state="active",
        ),
    ):
        assert adapter.validate_json(signal.model_dump_json()).model_dump() == signal.model_dump()
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "paid"})
    bootstrap = TypeAdapter(ResourceRecoveryBootstrap)
    for purpose in RecoveryPurpose:
        assert purpose.value in json.dumps(bootstrap.json_schema())
    assert (
        Invoice(
            id="inv_valid",
            status="partially_paid",
            amount_paise=100,
            amount_paid_paise=20,
            amount_due_paise=80,
            currency="INR",
        ).amount_due_paise
        == 80
    )
    with pytest.raises(ValidationError):
        Invoice(
            id="inv_invalid",
            status="paid",
            amount_paise=100,
            amount_paid_paise=20,
            amount_due_paise=80,
            currency="INR",
        )
    with pytest.raises(ValidationError):
        Subscription(id="sub_valid", plan_id="plan_valid", status="unknown")


def test_unrelated_customer_amount_success_cannot_close_registered_invoice(session_factory):
    from leakproof.services import PaidSignal, record_paid_signal

    with session_factory() as session:
        case, _ = record_risk(session, risk())
        unrelated = PaidSignal(
            merchant_id=SCOPE.merchant_id,
            customer_id=case.customer_id,
            entity_id="pay_unrelated",
            entity_root_id="order_unrelated",
            amount_paise=80,
            currency="INR",
            evidence={},
            occurred_at=NOW + timedelta(minutes=1),
        )
        assert record_paid_signal(session, unrelated) is None
        assert case.outcome is None
        assert session.scalar(select(func.count()).select_from(RecoveryAttribution)) == 0


def test_multi_resource_sessions_share_invoice_owner_and_keep_subscription_setup_separate(
    session_factory,
):
    from test_api_august_31 import settings

    from leakproof.demo.contracts import ResourceSessionCreated
    from leakproof.demo.security import issue_session_token
    from leakproof.provider_resources import case_for_session

    with session_factory() as session:
        case, _ = record_risk(session, risk())
        obligation = get_obligation(session, SCOPE, INVOICE, "INR")
        for index in range(2):
            demo = DemoSession(
                id=f"demo_invoice_{index}",
                merchant_id=SCOPE.merchant_id,
                customer_id=case.customer_id,
                scenario_type="INVOICE_OVERDUE",
                primary_entity_type="invoice",
                primary_entity_id=INVOICE.entity_id,
                amount_paise=80,
                currency="INR",
                state="AT_RISK",
                setup_state="READY",
                expires_at=NOW + timedelta(hours=1),
            )
            session.add(demo)
            session.flush()
            register_entity(session, SCOPE, INVOICE, session_id=demo.id, obligation=obligation)
            assert case_for_session(session, demo).id == case.id
            config = settings()
            projection = get_demo_session_projection(
                session,
                demo.id,
                settings=config,
                session_token=issue_session_token(
                    demo.id, demo.merchant_id, demo.expires_at, config.recovery_token_secret
                ),
                now=NOW,
            )
            assert projection.case.case_id == case.id
            assert not projection.recovery_url_available
            assert projection.capability_evidence == "ARCHITECTURE_READY"
        sub = DemoSession(
            id="demo_subscription",
            merchant_id=SCOPE.merchant_id,
            customer_id=case.customer_id,
            scenario_type="MANDATE_BROKEN",
            primary_entity_type="subscription",
            primary_entity_id="sub_setup",
            amount_paise=0,
            currency="INR",
            setup_state="ACTION_REQUIRED",
            state="CREATED",
            expires_at=NOW + timedelta(hours=1),
        )
        session.add(sub)
        session.flush()
        assert case_for_session(session, sub) is None
        adapter = TypeAdapter(ResourceSessionCreated)
        for resource_session in (demo, sub):
            value = adapter.validate_python(
                {
                    "primary_entity_type": resource_session.primary_entity_type,
                    "primary_entity_id": resource_session.primary_entity_id,
                    "scenario_type": resource_session.scenario_type,
                    "session_id": resource_session.id,
                    "session_token": "opaque",
                    "setup_state": resource_session.setup_state,
                    "amount_paise": resource_session.amount_paise,
                    "currency": resource_session.currency,
                    "expires_at": resource_session.expires_at,
                    "email_mode": "preview_only",
                }
            )
            assert value.primary_entity_type == resource_session.primary_entity_type


@pytest.mark.parametrize(
    "purpose,entity,scenario",
    [
        ("invoice_hosted_payment", INVOICE, "INVOICE_OVERDUE"),
        ("subscription_method_update", SUBSCRIPTION, "MANDATE_BROKEN"),
    ],
)
def test_payment_callback_rejects_other_token_purposes_before_fetch(
    session_factory, monkeypatch, purpose, entity, scenario
):
    from leakproof.demo.contracts import CheckoutPaymentVerificationRequest
    from leakproof.demo.rate_limit import InMemoryRateLimiter
    from leakproof.demo.service import DemoSessionUnauthorized, verify_checkout_payment

    with session_factory() as session:
        created, provider, config = create_session(session)
        config = config.model_copy(
            update={
                "razorpay_key_id": "rzp_test_contract",
                "razorpay_key_secret": "callback-test-secret",
            }
        )
        demo = session.get(DemoSession, created.session_id)
        token = issue_resource_recovery_token(
            RecoveryTokenClaims(
                version=2,
                session_id=demo.id,
                merchant_id=demo.merchant_id,
                scenario_type=scenario,
                entity=entity,
                purpose=purpose,
                amount_paise=demo.amount_paise,
                currency=demo.currency,
                expires_at=NOW + timedelta(minutes=5),
            ),
            config.recovery_token_secret,
        )

        def forbidden_fetch(_):
            pytest.fail("wrong-purpose token reached the payment provider")

        monkeypatch.setattr(provider, "fetch_payment", forbidden_fetch)
        with pytest.raises(DemoSessionUnauthorized):
            verify_checkout_payment(
                session,
                demo.id,
                CheckoutPaymentVerificationRequest(
                    razorpay_payment_id="pay_callback",
                    razorpay_order_id=demo.razorpay_order_id,
                    razorpay_signature="a" * 64,
                ),
                recovery_token=token,
                provider=provider,
                limiter=InMemoryRateLimiter(),
                settings=config,
                now=NOW,
            )


def test_authorization_and_zero_value_registration_cannot_change_invoice_balance():
    with pytest.raises(ValidationError, match="zero-value"):
        payment(amount=0, due=0)
    with pytest.raises(ValidationError, match="invoice balance"):
        EntityStateSignal(
            scope=SCOPE,
            entity=SUBSCRIPTION,
            source="razorpay_webhook",
            occurred_at=NOW,
            state="active",
            amount_due_paise=0,
        )
    with pytest.raises(ValidationError, match="invoice balance"):
        RecoverySignal(
            scope=SCOPE,
            entity=SUBSCRIPTION,
            source="razorpay_webhook",
            occurred_at=NOW,
            settlement="authorization_repaired",
            currency="INR",
            amount_due_paise=0,
        )


def test_success_first_legacy_order_history_never_credits_later_capture(session_factory):
    with session_factory() as session:
        created, _, config = create_session(session)
        for event, event_id, at in [
            ("order.paid", "evt_order_first", NOW + timedelta(seconds=2)),
            ("payment.failed", "evt_failure_late", NOW),
            ("payment.captured", "evt_capture_late", NOW + timedelta(seconds=2)),
        ]:
            process_payload(
                session,
                config.default_merchant_id,
                razorpay_payload(event, created.razorpay_order_id, occurred_at=at),
                event_id,
            )
        assert session.scalar(select(func.count()).select_from(RecoveryCase)) == 1
        assert session.scalar(select(RecoveryCase.outcome)) == "RECOVERED"
        assert session.scalar(select(func.count()).select_from(RecoveryAttribution)) == 0


def test_order_paid_without_payment_id_can_reconcile_through_existing_callback(session_factory):
    from leakproof.demo.contracts import CheckoutPaymentVerificationRequest
    from leakproof.demo.rate_limit import InMemoryRateLimiter
    from leakproof.demo.service import verify_checkout_payment
    from leakproof.providers import Payment

    with session_factory() as session:
        created, provider, config = create_session(session)
        config = config.model_copy(
            update={
                "razorpay_key_id": "rzp_test_contract",
                "razorpay_key_secret": "callback-test-secret",
            }
        )
        for event in ("payment.failed", "order.paid"):
            process_payload(
                session,
                config.default_merchant_id,
                razorpay_payload(event, created.razorpay_order_id),
                f"evt_{event}",
            )
        assert session.scalar(select(func.count()).select_from(RecoveryAttribution)) == 0
        provider.payments["pay_callback"] = Payment(
            "pay_callback", created.razorpay_order_id, created.amount_paise, "INR", "captured"
        )
        signature = hmac.new(
            config.razorpay_key_secret.encode(),
            f"{created.razorpay_order_id}|pay_callback".encode(),
            hashlib.sha256,
        ).hexdigest()
        request = CheckoutPaymentVerificationRequest(
            razorpay_payment_id="pay_callback",
            razorpay_order_id=created.razorpay_order_id,
            razorpay_signature=signature,
        )
        for _ in range(2):
            verify_checkout_payment(
                session,
                created.session_id,
                request,
                session_token=created.session_token,
                provider=provider,
                limiter=InMemoryRateLimiter(),
                settings=config,
                now=NOW + timedelta(minutes=1),
            )
        assert session.scalar(select(RecoveryAttribution.amount_paise)) == created.amount_paise
        assert session.scalar(select(func.count()).select_from(Settlement)) == 1
