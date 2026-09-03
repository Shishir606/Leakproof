"""Poll one live demo session and save its sanitized September 4 acceptance artifact."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument(
        "--scenario-type",
        choices=[
            "CHECKOUT_ABANDON",
            "PAYMENT_FAILURE",
            "INVOICE_OVERDUE",
            "SUBSCRIPTION_HALT",
        ],
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--token-env",
        default="LEAKPROOF_REHEARSAL_SESSION_TOKEN",
        help="Environment variable holding the session token (never written to the artifact).",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get(args.token_env, "")
    if not token:
        print(f"missing session token in {args.token_env}", file=sys.stderr)
        return 2
    if args.timeout_seconds < 0 or args.poll_seconds <= 0:
        print("timeout must be non-negative and polling must be positive", file=sys.stderr)
        return 2

    output = args.output or Path("artifacts/api-acceptance") / f"{args.session_id}.json"
    endpoint = f"{args.base_url.rstrip('/')}/demo/sessions/{args.session_id}/acceptance.json"
    deadline = time.monotonic() + args.timeout_seconds
    payload: dict | None = None
    with httpx2.Client(timeout=10.0) as client:
        while True:
            response = client.get(
                endpoint,
                headers={"x-leakproof-session-token": token},
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("passed") or time.monotonic() >= deadline:
                break
            time.sleep(args.poll_seconds)

    if args.scenario_type and (
        (payload or {}).get("session", {}).get("scenario_type") != args.scenario_type
        or ((payload or {}).get("case") or {}).get("leak_type") != args.scenario_type
    ):
        print(
            "requested scenario did not finish with the expected detected case type",
            file=sys.stderr,
        )
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"saved sanitized acceptance artifact to {output}")
    if payload and payload.get("passed"):
        print("all blocking acceptance checks passed")
        return 0
    failed = [
        item["check"]
        for item in (payload or {}).get("checks", [])
        if item.get("severity") == "blocking" and not item.get("passed")
    ]
    print("blocking checks still open: " + ", ".join(failed), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
