from __future__ import annotations

from datetime import UTC, datetime, timedelta

from leakproof.config import get_measurement_config
from leakproof.diagnosis import diagnose_case
from leakproof.models.db import BatchRun, Merchant
from leakproof.models.domain import Arm, LeakType
from leakproof.policy import plan_case
from leakproof.services import NormalizedSignal, record_signal

NOW = datetime(2026, 9, 2, 4, 30, tzinfo=UTC)


def _dashboard_case(session):
    run_id = "run_dashboard"
    case, _ = record_signal(
        session,
        NormalizedSignal(
            merchant_id="merchant_dashboard",
            customer_id="customer_dashboard",
            leak_type=LeakType.PAYMENT_FAILURE,
            entity_type="payment",
            entity_id="pay_dashboard",
            entity_root_id="order_dashboard",
            amount_at_risk=245_000,
            currency="INR",
            evidence={
                "error_source": "bank",
                "error_step": "payment_authorization",
                "error_reason": "gateway_technical_error",
                "simulation": {"synthetic": True, "run_id": run_id},
            },
            occurred_at=NOW,
        ),
    )
    measurement = get_measurement_config()
    session.add(
        BatchRun(
            id=run_id,
            merchant_id=case.merchant_id,
            started_at=NOW,
            completed_at=NOW + timedelta(minutes=8),
            holdout_seed=measurement.holdout.seed,
            holdout_fraction=measurement.holdout.fraction,
            measurement_config=measurement.model_dump(mode="json"),
        )
    )
    case.arm = Arm.TREATMENT.value
    session.get(Merchant, case.merchant_id).policy = {"synthetic": True}
    diagnose_case(session, case.id)
    plan_case(session, case.id, now=NOW)
    session.commit()
    return case


def test_case_list_filters_and_exposes_dashboard_summary(client, session_factory):
    with session_factory() as session:
        case = _dashboard_case(session)
        case_id = case.id

    response = client.get(
        "/cases",
        params={"leak_type": "PAYMENT_FAILURE", "batch_run_id": "run_dashboard"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0] == {
        **payload["items"][0],
        "id": case_id,
        "batch_run_id": "run_dashboard",
        "leak_type": "PAYMENT_FAILURE",
        "amount_at_risk": 245_000,
        "arm": "TREATMENT",
    }
    assert payload["items"][0]["event_count"] >= 4

    empty = client.get("/cases", params={"state": "SUPPRESSED"})
    assert empty.status_code == 200
    assert empty.json()["total"] == 0


def test_case_detail_contains_diagnosis_plan_and_audit_timeline(client, session_factory):
    with session_factory() as session:
        case_id = _dashboard_case(session).id

    response = client.get(f"/cases/{case_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["case"]["id"] == case_id
    assert payload["replay"]["case"]["id"] == case_id
    assert payload["replay"]["projection_matches"] is True
    assert payload["diagnosis"]["rule_id"] == "T1_TRANSIENT"
    assert payload["actions"]
    assert [event["kind"] for event in payload["replay"]["events"]] == [
        "DETECTED",
        "ASSIGNED",
        "DIAGNOSED",
        "PLANNED",
    ]

    audit = client.get(f"/cases/{case_id}/audit.json")
    assert audit.status_code == 200
    assert audit.json()["events"] == payload["replay"]["events"]


def test_latest_scoreboard_drives_recordable_dashboard(client, session_factory):
    with session_factory() as session:
        _dashboard_case(session)

    response = client.get("/scoreboard/latest")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == "run_dashboard"
    assert payload["data_provenance"] == "SIMULATED_END_TO_END"
    assert payload["cases_processed"] == 1
    assert payload["cases_by_leak_type"] == {"PAYMENT_FAILURE": 1}


def test_dashboard_endpoints_return_not_found_without_data(client):
    assert client.get("/cases/case_missing").status_code == 404
    assert client.get("/cases/case_missing/audit.json").status_code == 404
    assert client.get("/scoreboard/latest").status_code == 404
