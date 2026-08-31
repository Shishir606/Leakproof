"""Exercise the August 25 acceptance criteria against the running Compose stack."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from typing import Any

import psycopg

from leakproof.config import get_settings

API_URL = os.environ.get("LEAKPROOF_VERIFY_API_URL", "http://localhost:8000")
DATABASE_URL = os.environ.get(
    "LEAKPROOF_VERIFY_DATABASE_URL",
    "postgresql://leakproof:leakproof@localhost:55432/leakproof",
)


def request_json(path: str, **kwargs: Any) -> dict[str, Any]:
    request = urllib.request.Request(f"{API_URL}{path}", **kwargs)
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def operator_headers() -> dict[str, str]:
    token = get_settings().operator_api_token
    if not token:
        raise RuntimeError(
            "LEAKPROOF_OPERATOR_API_TOKEN is required to verify the protected replay endpoint"
        )
    return {"Authorization": f"Bearer {token}"}


def post_failure(run_id: str, attempt: int) -> dict[str, Any]:
    payload = {
        "event": "payment.failed",
        "created_at": 1787625000 + attempt,
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_verify_{run_id}_{attempt}",
                    "order_id": f"order_verify_{run_id}",
                    "customer_id": f"customer_verify_{run_id}",
                    "amount": 125_000,
                    "currency": "INR",
                    "error_source": "bank",
                    "error_step": "payment_authorization",
                    "error_reason": "gateway_technical_error",
                }
            }
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(
        get_settings().razorpay_webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return request_json(
        "/webhooks/razorpay",
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-razorpay-signature": signature,
            "x-razorpay-event-id": f"rzp_evt_verify_{run_id}_{attempt}",
            "x-leakproof-merchant-id": "merchant_demo",
        },
    )


def safe_error_summary(value: str | None) -> str | None:
    if not value:
        return None
    summary = value.splitlines()[0][:240]
    summary = re.sub(r"(?i)bearer\s+\S+", "Bearer [REDACTED]", summary)
    summary = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]", summary)
    summary = re.sub(
        r"(?i)(token|secret|api[_-]?key)(\s*[:=]\s*)\S+",
        r"\1\2[REDACTED]",
        summary,
    )
    return summary


def webhook_diagnostics(connection: psycopg.Connection, run_id: str) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*),
                   count(*) FILTER (WHERE processed_at IS NOT NULL),
                   coalesce(sum(processing_attempts), 0),
                   array_remove(array_agg(last_error), NULL)
            FROM webhook_events
            WHERE merchant_id = %s AND provider_event_key LIKE %s
            """,
            ("merchant_demo", f"rzp_evt_verify_{run_id}_%"),
        )
        inbox_count, processed_count, processing_attempts, errors = cursor.fetchone()
        cursor.execute(
            """
            SELECT coalesce(array_agg(events.kind ORDER BY events.seq), ARRAY[]::varchar[])
            FROM cases
            LEFT JOIN events ON events.case_id = cases.id
            WHERE cases.merchant_id = %s AND cases.dedupe_key = %s
            """,
            ("merchant_demo", f"pf:customer_verify_{run_id}:order_verify_{run_id}"),
        )
        event_kinds = list(cursor.fetchone()[0])
    connection.commit()
    return {
        "inbox_count": int(inbox_count),
        "processed_count": int(processed_count),
        "event_kinds": event_kinds,
        "processing_attempts": int(processing_attempts),
        "last_errors": [safe_error_summary(error) for error in (errors or [])][-3:],
    }


def wait_for_processed_webhooks(
    connection: psycopg.Connection, run_id: str
) -> dict[str, Any]:
    deadline = time.monotonic() + 15
    diagnostics: dict[str, Any] = {}
    while time.monotonic() < deadline:
        diagnostics = webhook_diagnostics(connection, run_id)
        if diagnostics["inbox_count"] == 3 and diagnostics["processed_count"] == 3:
            return diagnostics
        time.sleep(0.1)
    raise TimeoutError(
        "worker did not process three distinct webhook rows within 15 seconds: "
        + json.dumps(diagnostics, sort_keys=True)
    )


def fetch_case_events(
    connection: psycopg.Connection, run_id: str
) -> tuple[str, list[str]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id
            FROM cases
            WHERE merchant_id = %s AND dedupe_key = %s
            """,
            ("merchant_demo", f"pf:customer_verify_{run_id}:order_verify_{run_id}"),
        )
        cases = cursor.fetchall()
        if len(cases) != 1:
            raise AssertionError(f"expected one case, found {len(cases)}")
        case_id = cases[0][0]
        cursor.execute(
            "SELECT kind FROM events WHERE case_id = %s ORDER BY seq",
            (case_id,),
        )
        event_kinds = [row[0] for row in cursor.fetchall()]
    connection.commit()
    return case_id, event_kinds


def assert_semantic_sequence(event_kinds: list[str]) -> None:
    required = ["DETECTED", "ASSIGNED", "SIGNAL", "SIGNAL"]
    if event_kinds != required:
        raise AssertionError(
            f"expected semantic event sequence {required}, received {event_kinds}"
        )


def assert_database_rejects_event_mutations(
    connection: psycopg.Connection, case_id: str
) -> None:
    statements = (
        "UPDATE events SET kind = kind WHERE case_id = %s",
        "DELETE FROM events WHERE case_id = %s",
    )
    for statement in statements:
        try:
            with connection.cursor() as cursor:
                cursor.execute(statement, (case_id,))
        except psycopg.Error as exc:
            connection.rollback()
            if "append-only" not in str(exc):
                raise AssertionError("event mutation failed for an unexpected reason") from exc
        else:
            connection.rollback()
            raise AssertionError("PostgreSQL allowed an audit event to be changed")


def main() -> None:
    readiness = request_json("/health/ready")
    if readiness != {"status": "ready"}:
        raise AssertionError(f"API is not ready: {readiness}")

    run_id = secrets.token_hex(4)
    first = post_failure(run_id, 0)
    duplicate = post_failure(run_id, 0)
    if first["duplicate"] or not duplicate["duplicate"]:
        raise AssertionError("duplicate provider event was not identified")
    if first["webhook_id"] != duplicate["webhook_id"]:
        raise AssertionError("duplicate provider event created a second inbox record")

    for attempt in (1, 2):
        response = post_failure(run_id, attempt)
        if response["duplicate"]:
            raise AssertionError(f"distinct payment failure {attempt} was deduplicated")

    with psycopg.connect(DATABASE_URL) as connection:
        diagnostics = wait_for_processed_webhooks(connection, run_id)
        case_id, event_kinds = fetch_case_events(connection, run_id)
        assert_semantic_sequence(event_kinds)
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            migration = cursor.fetchone()[0]
        assert_database_rejects_event_mutations(connection, case_id)

    replay = request_json(f"/cases/{case_id}/replay", headers=operator_headers())
    if not replay["projection_matches"]:
        raise AssertionError("case projection does not match the replayed event timeline")
    assert_semantic_sequence([event["kind"] for event in replay["events"]])

    print(
        json.dumps(
            {
                "status": "passed",
                "migration": migration,
                "case_id": case_id,
                "unique_webhooks_processed": diagnostics["processed_count"],
                "case_events": len(event_kinds),
                "event_kinds": event_kinds,
                "duplicate_webhook_rejected": True,
                "append_only_enforced_by_postgres": True,
                "projection_replay_matches": True,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
