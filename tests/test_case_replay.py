from datetime import UTC, datetime

import pytest

from leakproof.audit.timeline import replay_case
from leakproof.models.db import Event
from leakproof.models.domain import CaseState, LeakType
from leakproof.services import NormalizedSignal, record_signal


def payment_failure_signal() -> NormalizedSignal:
    return NormalizedSignal(
        merchant_id="merchant_1",
        customer_id="customer_1",
        leak_type=LeakType.PAYMENT_FAILURE,
        entity_type="payment",
        entity_id="pay_1",
        entity_root_id="order_1",
        amount_at_risk=125_000,
        currency="INR",
        evidence={"error_reason": "insufficient_funds"},
        occurred_at=datetime(2026, 8, 25, 8, 0, tzinfo=UTC),
    )


def test_case_can_be_created_and_replayed(session_factory):
    with session_factory() as session:
        case, created = record_signal(session, payment_failure_signal())
        session.commit()

        replayed = replay_case(session, case.id)

    assert created is True
    assert replayed.replayed_state == CaseState.DETECTED
    assert replayed.case.amount_at_risk == 125_000
    assert [event.kind for event in replayed.events] == ["DETECTED", "ASSIGNED"]
    assert replayed.events[1].payload["stratify_by"] == ["leak_type", "amount_band"]
    assert replayed.projection_matches is True


def test_public_replay_endpoint_returns_case_and_ordered_events(client, session_factory):
    with session_factory() as session:
        case, _ = record_signal(session, payment_failure_signal())
        record_signal(session, payment_failure_signal())
        session.commit()
        case_id = case.id

    response = client.get(f"/cases/{case_id}/replay")

    assert response.status_code == 200
    replayed = response.json()
    assert replayed["case"]["id"] == case_id
    assert replayed["replayed_state"] == "DETECTED"
    assert replayed["projection_matches"] is True
    assert replayed["case"]["detected_at"].endswith("Z")
    assert replayed["events"][0]["occurred_at"].endswith("Z")
    assert [(event["seq"], event["kind"]) for event in replayed["events"]] == [
        (1, "DETECTED"),
        (2, "ASSIGNED"),
        (3, "SIGNAL"),
    ]


def test_public_replay_endpoint_returns_not_found_for_unknown_case(client):
    response = client.get("/cases/case_missing/replay")

    assert response.status_code == 404
    assert response.json() == {"detail": "case not found"}


@pytest.mark.parametrize("mutation", ["update", "delete"])
def test_case_events_cannot_be_mutated_through_the_orm(session_factory, mutation):
    with session_factory() as session:
        case, _ = record_signal(session, payment_failure_signal())
        session.commit()
        stored_event = session.query(Event).filter_by(case_id=case.id, seq=1).one()

        if mutation == "update":
            stored_event.kind = "CLOSED"
        else:
            session.delete(stored_event)

        with pytest.raises(ValueError, match="append-only"):
            session.commit()
