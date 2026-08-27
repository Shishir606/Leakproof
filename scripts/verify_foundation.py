"""Exercise the August 25 acceptance criteria against the running Compose stack."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
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


def wait_for_case(connection: psycopg.Connection, run_id: str) -> tuple[str, int, int]:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT cases.id,
                       (SELECT count(*) FROM events WHERE events.case_id = cases.id),
                       (SELECT count(*) FROM webhook_events
                        WHERE provider_event_key LIKE %s AND processed_at IS NOT NULL)
                FROM cases
                WHERE merchant_id = %s AND dedupe_key = %s
                """,
                (
                    f"rzp_evt_verify_{run_id}_%",
                    "merchant_demo",
                    f"pf:customer_verify_{run_id}:order_verify_{run_id}",
                ),
            )
            result = cursor.fetchone()
        connection.commit()
        if result is not None and result[1] == 3 and result[2] == 3:
            return result
        time.sleep(0.1)
    raise TimeoutError("Celery did not process three webhook events within 15 seconds")


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
        case_id, event_count, processed_count = wait_for_case(connection, run_id)
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            migration = cursor.fetchone()[0]
        assert_database_rejects_event_mutations(connection, case_id)

    replay = request_json(f"/cases/{case_id}/replay")
    if not replay["projection_matches"]:
        raise AssertionError("case projection does not match the replayed event timeline")
    if [event["kind"] for event in replay["events"]] != ["DETECTED", "SIGNAL", "SIGNAL"]:
        raise AssertionError("replay did not return the expected ordered event timeline")

    print(
        json.dumps(
            {
                "status": "passed",
                "migration": migration,
                "case_id": case_id,
                "unique_webhooks_processed": processed_count,
                "case_events": event_count,
                "duplicate_webhook_rejected": True,
                "append_only_enforced_by_postgres": True,
                "projection_replay_matches": True,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
