from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from leakproof.api.access_logging import RecoveryCapabilityFilter, redact_recovery_target
from leakproof.api.app import app
from leakproof.api.auth import OperatorPrincipal, get_operator_principal
from leakproof.models.domain import LeakType
from leakproof.services import NormalizedSignal, record_signal


def _case(session, merchant_id: str, suffix: str) -> str:
    case, _ = record_signal(
        session,
        NormalizedSignal(
            merchant_id=merchant_id,
            customer_id=f"customer_{suffix}",
            leak_type=LeakType.PAYMENT_FAILURE,
            entity_type="payment",
            entity_id=f"pay_{suffix}",
            entity_root_id=f"order_{suffix}",
            amount_at_risk=50_000,
            currency="INR",
            evidence={"error_reason": "gateway_technical_error"},
            occurred_at=datetime(2026, 8, 31, tzinfo=UTC),
        ),
    )
    session.commit()
    return case.id


@pytest.mark.parametrize(
    "path",
    [
        "/cases",
        "/scoreboard/latest",
        "/evals/latest",
        "/costs",
        "/suppressions",
    ],
)
def test_operational_collections_reject_missing_and_invalid_credentials(client, path: str):
    missing = client.get(path, headers={"Authorization": ""})
    invalid = client.get(path, headers={"Authorization": "Bearer wrong-token"})

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/cases/missing", None),
        ("get", "/cases/missing/replay", None),
        ("get", "/cases/missing/audit.json", None),
        ("get", "/scoreboard/missing", None),
        ("get", "/scoreboard/missing/exceptions", None),
        ("post", "/batch/run", {"seed": 42}),
        ("post", "/suppressions/1/close", None),
        (
            "post",
            "/actions/missing/voice/turns",
            {"provider_turn_id": "turn-1", "transcript": "hello"},
        ),
    ],
)
def test_operational_object_and_mutation_routes_require_operator(
    client, method: str, path: str, payload: dict | None
):
    response = client.request(method, path, json=payload, headers={"Authorization": ""})

    assert response.status_code == 401


def test_valid_operator_is_scoped_and_object_routes_hide_existence(client, session_factory):
    with session_factory() as session:
        allowed_case = _case(session, "merchant_allowed", "allowed")
        hidden_case = _case(session, "merchant_hidden", "hidden")

    app.dependency_overrides[get_operator_principal] = lambda: OperatorPrincipal(
        merchant_ids=frozenset({"merchant_allowed"})
    )
    try:
        listing = client.get("/cases")
        assert listing.status_code == 200
        assert [item["id"] for item in listing.json()["items"]] == [allowed_case]
        assert client.get(f"/cases/{hidden_case}").status_code == 404
        assert client.get(f"/cases/{hidden_case}/replay").status_code == 404
        assert client.get(f"/cases/{hidden_case}/audit.json").status_code == 404
    finally:
        app.dependency_overrides.pop(get_operator_principal, None)


def test_public_capability_and_demo_boundaries_do_not_require_operator_token(client):
    capabilities = client.get("/capabilities", headers={"Authorization": ""})
    created = client.post("/demo/sessions", json={}, headers={"Authorization": ""})

    assert capabilities.status_code == 200
    assert capabilities.json()["headline"] == (
        "one live recovery loop; four simulated expansion surfaces"
    )
    assert created.status_code == 201
    projection = client.get(
        f"/demo/sessions/{created.json()['session_id']}",
        headers={
            "Authorization": "",
            "x-leakproof-session-token": created.json()["session_token"],
        },
    )
    assert projection.status_code == 200
    assert projection.json()["data_provenance"] == "SIMULATED_END_TO_END"


def test_session_token_cannot_cross_demo_sessions(client):
    first = client.post("/demo/sessions", json={}).json()
    second = client.post("/demo/sessions", json={}).json()

    response = client.get(
        f"/demo/sessions/{second['session_id']}",
        headers={"x-leakproof-session-token": first["session_token"]},
    )

    assert response.status_code == 401


def test_operator_token_is_absent_from_public_json_and_built_client_assets(client):
    token = "test-operator-token-that-is-at-least-32-bytes"
    assert token not in client.get("/capabilities", headers={"Authorization": ""}).text

    build_dir = Path(__file__).parents[1] / "dashboard" / ".next"
    if build_dir.exists():
        token_bytes = token.encode()
        for path in build_dir.rglob("*"):
            if path.is_file():
                assert token_bytes not in path.read_bytes(), path


def test_dashboard_proxy_forwards_recovery_authorization_header():
    proxy_source = (
        Path(__file__).parents[1] / "dashboard" / "lib" / "backend-proxy.ts"
    ).read_text(encoding="utf-8")

    assert 'request.headers.get("x-leakproof-recovery-token")' in proxy_source
    assert 'headers.set("x-leakproof-recovery-token", recoveryToken)' in proxy_source


def test_recovery_capability_is_redacted_from_uvicorn_access_logs():
    token = "signed-capability.payload-signature"
    record = __import__("logging").LogRecord(
        "uvicorn.access",
        20,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", f"/recover/{token}?source=email", "1.1", 200),
        None,
    )

    assert RecoveryCapabilityFilter().filter(record)
    rendered = record.getMessage()
    assert token not in rendered
    assert "/recover/[REDACTED]?source=email" in rendered
    assert redact_recovery_target("/health/ready") == "/health/ready"
