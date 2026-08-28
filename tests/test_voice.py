from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from leakproof.actuators import execute_action
from leakproof.diagnosis import diagnose_case
from leakproof.models.db import (
    Action,
    ActuatorReceipt,
    Consent,
    Contact,
    Customer,
    Event,
    Merchant,
    Promise,
    VoiceTurn,
)
from leakproof.models.domain import Arm, LeakType
from leakproof.policy import plan_case
from leakproof.services import NormalizedSignal, record_signal
from leakproof.voice import handle_voice_turn

NOW = datetime(2026, 9, 3, 4, 30, tzinfo=UTC)  # 10:00 IST


def _voice_case(session, suffix: str):
    case, _ = record_signal(
        session,
        NormalizedSignal(
            merchant_id=f"merchant_voice_{suffix}",
            customer_id=f"customer_voice_{suffix}",
            leak_type=LeakType.INVOICE_OVERDUE,
            entity_type="invoice",
            entity_id=f"invoice_voice_{suffix}",
            entity_root_id=None,
            amount_at_risk=4_000_000,
            currency="INR",
            evidence={"days_overdue": 12, "payer_behavior": "SLOW_BUT_GOOD"},
            occurred_at=NOW,
        ),
    )
    case.arm = Arm.TREATMENT.value
    merchant = session.get(Merchant, case.merchant_id)
    merchant.policy = {"standing_merchant_approval": True}
    session.add(
        Consent(
            customer_id=case.customer_id,
            channel="voice",
            granted=True,
            basis="recorded_test_consent",
            recorded_at=NOW,
        )
    )
    diagnose_case(session, case.id)
    plan = plan_case(session, case.id, now=NOW)
    voice = session.scalar(
        select(Action).where(
            Action.case_id == case.id,
            Action.action_type == "voice_hinglish",
        )
    )
    assert voice is not None
    for prior in session.scalars(
        select(Action).where(
            Action.case_id == case.id,
            Action.step_index < voice.step_index,
        )
    ):
        prior.status = "succeeded"
    voice.scheduled_for = NOW
    session.commit()
    result = execute_action(session, voice.id, now=voice.scheduled_for)
    assert result.status == "succeeded"
    receipt = session.get(ActuatorReceipt, voice.idempotency_key)
    assert receipt.request["message"]["template_id"] == "util_recovery_voice_v1"
    return case, voice, plan


def test_two_turn_voice_dialogue_captures_one_idempotent_promise(session_factory):
    with session_factory() as session:
        case, voice, _ = _voice_case(session, "promise")

        first = handle_voice_turn(
            session,
            voice.id,
            provider_turn_id="voice_turn_promise_1",
            transcript="Haan ji, main hi speaking.",
            occurred_at=NOW,
        )
        second = handle_voice_turn(
            session,
            voice.id,
            provider_turn_id="voice_turn_promise_2",
            transcript="Main 10 September ko payment kar dunga.",
            occurred_at=NOW,
        )
        replay = handle_voice_turn(
            session,
            voice.id,
            provider_turn_id="voice_turn_promise_2",
            transcript="ignored duplicate delivery",
            occurred_at=NOW,
        )

        promise = session.scalar(select(Promise).where(Promise.case_id == case.id))
        assert first.intent == "IDENTITY_CONFIRMED"
        assert first.ended is False
        assert first.message.template_id == "util_voice_payment_options_v1"
        assert second.intent == "PROMISE_TO_PAY"
        assert second.ended is True
        assert second.message.template_id == "util_voice_promise_confirm_v1"
        assert promise.promised_on.isoformat() == "2026-09-10"
        assert promise.amount_paise == case.amount_at_risk
        assert promise.captured_via == "voice"
        assert replay.replayed is True
        assert replay.promise_id == promise.id
        assert session.scalar(select(func.count()).select_from(VoiceTurn)) == 2
        assert session.scalar(select(func.count()).select_from(Promise)) == 1
        assert list(
            session.scalars(
                select(Event.kind)
                .where(Event.case_id == case.id)
                .order_by(Event.seq.desc())
                .limit(3)
            )
        ) == ["PROMISE_CAPTURED", "VOICE_TURN", "VOICE_TURN"]


def test_voice_opt_out_ends_immediately_and_cancels_later_contact(session_factory):
    with session_factory() as session:
        case, voice, _ = _voice_case(session, "optout")
        later = Action(
            id="action_after_optout",
            case_id=case.id,
            step_index=voice.step_index + 1,
            action_type="human_handoff",
            scheduled_for=voice.scheduled_for,
            status="pending",
            cost_paise=0,
        )
        session.add(later)
        session.commit()

        reply = handle_voice_turn(
            session,
            voice.id,
            provider_turn_id="voice_turn_optout_1",
            transcript="Please dobara call mat karna, opt out.",
            occurred_at=NOW,
        )

        assert reply.intent == "OPT_OUT"
        assert reply.ended is True
        assert reply.turn_number == 1
        assert reply.message.template_id == "util_voice_optout_v1"
        assert case.customer_id
        assert session.get(Customer, case.customer_id).dnc is True
        assert later.status == "cancelled"
        assert session.scalar(select(func.count()).select_from(Contact)) == 1


def test_voice_rejects_ungated_actions_and_ends_after_two_unknown_turns(session_factory):
    with session_factory() as session:
        case, voice, _ = _voice_case(session, "bounded")
        first = handle_voice_turn(
            session,
            voice.id,
            provider_turn_id="voice_turn_bounded_1",
            transcript="maybe something else",
            occurred_at=NOW,
        )
        second = handle_voice_turn(
            session,
            voice.id,
            provider_turn_id="voice_turn_bounded_2",
            transcript="still unclear",
            occurred_at=NOW,
        )

        assert first.ended is False
        assert second.ended is True
        assert second.message.template_id == "util_voice_human_v1"
        with pytest.raises(ValueError, match="already ended"):
            handle_voice_turn(
                session,
                voice.id,
                provider_turn_id="voice_turn_bounded_3",
                transcript="third turn",
                occurred_at=NOW,
            )


def test_voice_api_returns_registered_reply_and_case_exposes_promise(
    client, session_factory
):
    with session_factory() as session:
        case, voice, _ = _voice_case(session, "api")
        case_id, action_id = case.id, voice.id

    first = client.post(
        f"/actions/{action_id}/voice/turns",
        json={
            "provider_turn_id": "voice_turn_api_1",
            "transcript": "Haan, speaking",
            "occurred_at": NOW.isoformat(),
        },
    )
    second = client.post(
        f"/actions/{action_id}/voice/turns",
        json={
            "provider_turn_id": "voice_turn_api_2",
            "transcript": "I will pay tomorrow",
            "occurred_at": NOW.isoformat(),
        },
    )

    assert first.status_code == 200
    assert first.json()["reply_template_id"] == "util_voice_payment_options_v1"
    assert second.status_code == 200
    assert second.json()["intent"] == "PROMISE_TO_PAY"
    assert second.json()["ended"] is True
    detail = client.get(f"/cases/{case_id}")
    assert detail.status_code == 200
    assert detail.json()["promises"] == [
        {
            "id": second.json()["promise_id"],
            "case_id": case_id,
            "promised_on": "2026-09-04",
            "amount_paise": 4_000_000,
            "captured_via": "voice",
            "kept": None,
            "transcript_ref": "voice_turn_api_2",
        }
    ]
