"""Exercise the real dashboard with intercepted contracts; never contacts Razorpay.

Start the dashboard separately. Screenshots are watermarked fixture evidence, and the
summary explicitly leaves human provider rehearsal pending. No live acceptance is fabricated.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

SDK = """
window.Razorpay = class {
  constructor(options) { this.options = options; this.events = {}; }
  on(name, callback) { this.events[name] = callback; }
  open() {
    window.contractOrders = [...(window.contractOrders || []), this.options.order_id];
    const dialog = document.createElement('dialog');
    dialog.innerHTML = '<h2>Contract Checkout (fixture)</h2>' +
      '<button>Dismiss twice</button><button>Submit fixture</button><button>Fail fixture</button>';
    document.body.appendChild(dialog); dialog.showModal();
    const buttons = dialog.querySelectorAll('button');
    buttons[0].onclick = () => {
      dialog.remove(); this.options.modal.ondismiss(); this.options.modal.ondismiss();
    };
    buttons[1].onclick = () => {
      dialog.remove(); this.options.handler({razorpay_order_id: this.options.order_id,
        razorpay_payment_id: 'pay_contract_only', razorpay_signature: 'fixture-only'});
    };
    buttons[2].onclick = () => {
      dialog.remove(); this.events['payment.failed']({error: {description: 'Fixture failure'}});
      this.options.modal.ondismiss();
    };
  }
};
"""
WATERMARK = """
document.addEventListener('DOMContentLoaded', () => {
  const banner = document.createElement('div');
  banner.textContent = 'AUTOMATED UI CONTRACT • simulated provider responses • no Razorpay payment';
  banner.style.cssText = 'position:fixed;bottom:0;left:0;right:0;padding:10px;' +
    'background:#39254c;color:white;text-align:center;font:14px sans-serif;z-index:2147483647';
  document.body.appendChild(banner);
});
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:3100")
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/track-a/browser"))
    args = parser.parse_args()
    if urlparse(args.base_url).hostname not in {"127.0.0.1", "localhost"}:
        parser.error("Use an isolated local dashboard for these intercepted browser checks")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    session = {
        "session_id": "browser-contract-session",
        "session_token": "contract-session-token",
        "scenario_type": "CHECKOUT_ABANDON",
        "primary_entity_type": "order",
        "setup_state": "READY",
        "razorpay_key_id": "rzp_test_contract",
        "razorpay_order_id": "order_contract_original",
        "amount_paise": 50000,
        "currency": "INR",
        "email_mode": "preview_only",
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
    }
    state = {
        "phase": "idle",
        "block_telemetry": False,
        "expired": False,
        "creates": 0,
        "bootstrap_checks": 0,
        "bootstrap_blocked": False,
        "verification_calls": 0,
    }
    events: dict[str, dict] = {}
    dismissal_requests: list[str] = []
    checks: list[str] = []
    errors: list[str] = []

    def projection():
        phase = state["phase"]
        has_case = phase in {"confirmed", "recovered", "payment_failure"}
        recovered = phase == "recovered"
        metrics = {
            "cases_detected": int(has_case),
            "recovered_cases": int(recovered),
            "recovered_amount_paise": 50000 if recovered else 0,
            "recovery_rate": int(recovered),
            "median_recovery_time_seconds": 25 if recovered else None,
            "provider_failures": 0,
            "luna_cost_paise": 0,
        }
        return {
            **session,
            # UI contract fixture only: the watermark and summary disallow provider claims.
            "data_provenance": "LIVE_TELEMETRY_PROVIDER_RECONCILED"
            if has_case
            else "LIVE_PROVIDER_VERIFIED",
            "capability_evidence": "ARCHITECTURE_READY",
            "state": "RECOVERED" if recovered else "AT_RISK" if has_case else "CHECKOUT_OPEN",
            "case": {
                "case_id": "contract-one-case",
                "leak_type": "PAYMENT_FAILURE"
                if phase == "payment_failure"
                else "CHECKOUT_ABANDON",
                "state": "CLOSED" if recovered else "ACTING",
                "deterministic_diagnosis": {
                    "failure_class": "FRICTION",
                    "rule_id": "T1_CHECKOUT_FRICTION",
                    "confidence": 1,
                },
                "insight_status": "fallback",
                "insight": None,
            }
            if has_case
            else None,
            "abandonment_check": {
                "status": phase,
                "unpaid_confirmed": has_case,
                "due_at": (
                    datetime.now(UTC) + timedelta(seconds=7 if phase == "waiting" else -1)
                ).isoformat(),
                "browser_dismissed_at": now.isoformat() if events else None,
            },
            "recovery_url_available": has_case and not recovered,
            "recovery_path": "/recover/contract-token" if has_case and not recovered else None,
            "recovery_actions": [
                {
                    "action_type": "recovery_link",
                    "status": "completed" if recovered else "available",
                    "scheduled_for": now.isoformat(),
                    "gate_verdict": "NOT_REQUIRED",
                },
                {
                    "action_type": "email_link",
                    "status": "cancelled" if recovered else "pending",
                    "scheduled_for": now.isoformat(),
                    "gate_verdict": None,
                },
            ]
            if has_case
            else [],
            "provider_statuses": [],
            "timeline": [
                {
                    "kind": event["event_type"],
                    "source": "browser",
                    "occurred_at": now.isoformat(),
                    "payload": {},
                }
                for event in events.values()
            ],
            "metrics": metrics,
            "environment_metrics": metrics,
            "end_to_end_latency_seconds": 25,
            "gate_verdict": None,
        }

    def route_request(route):
        request = route.request
        path = urlparse(request.url).path
        if request.url == "https://checkout.razorpay.com/v1/checkout.js":
            route.fulfill(content_type="application/javascript", body=SDK)
            return
        if not request.url.startswith(args.base_url):
            route.abort()
            return
        if not path.startswith("/api/"):
            route.continue_()
            return
        if path == "/api/demo/sessions" and request.method == "POST":
            assert request.post_data_json["scenario_type"] == session["scenario_type"]
            state["creates"] += 1
            payload = session
        elif path.endswith("/checkout-events"):
            event = request.post_data_json
            assert request.headers.get("x-leakproof-session-token") == session["session_token"]
            if event["event_type"] == "checkout_dismissed":
                dismissal_requests.append(event["client_event_id"])
                if state["block_telemetry"]:
                    route.abort()
                    return
                if state["phase"] == "idle":
                    state["phase"] = "waiting"
            duplicate = event["client_event_id"] in events
            events[event["client_event_id"]] = event
            payload = {"accepted": True, "duplicate": duplicate, "event_id": len(events)}
        elif path.endswith("/payments/verify"):
            assert request.post_data_json["razorpay_order_id"] == session["razorpay_order_id"]
            state["verification_calls"] += 1
            state["phase"] = "recovered"
            payload = {
                "verified": True,
                "duplicate": False,
                "state": "RECOVERED",
                "payment_status": "captured",
            }
        elif path.startswith("/api/recover/"):
            state["bootstrap_checks"] += 1
            if state["bootstrap_blocked"]:
                route.fulfill(
                    status=409,
                    json={
                        "error": {
                            "code": "order_not_available",
                            "message": "Original order is already paid",
                            "retryable": False,
                        }
                    },
                )
                return
            payload = {**session, "purpose": "order_checkout"}
        elif path.endswith("/acceptance.json"):
            # Download the real backend TEST export, whose provenance remains synthetic.
            payload = json.loads(
                (args.output_dir.parent / "contract/contract-checkout_dismissal.json").read_text()
            )
        elif path == f"/api/demo/sessions/{session['session_id']}":
            if state["expired"]:
                route.fulfill(
                    status=410,
                    json={
                        "error": {
                            "code": "session_expired",
                            "message": "Session expired",
                            "retryable": False,
                        }
                    },
                )
                return
            payload = projection()
        else:
            route.fulfill(status=404, json={})
            return
        route.fulfill(json=payload)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1440, "height": 1100}, accept_downloads=True
        )
        context.add_init_script(WATERMARK)
        context.route("**/*", route_request)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))

        def capture(name):
            page.screenshot(path=str(args.output_dir / f"{name}.png"), full_page=True)

        page.goto(args.base_url + "/demo")
        expect(page.get_by_role("radio", name="Checkout abandonment", exact=True)).to_be_checked()
        capture("01-entry")
        page.get_by_role("button", name="Start checkout abandonment").click()
        expect(page.get_by_role("heading", name="Contract Checkout (fixture)")).to_be_visible()
        state["block_telemetry"] = True
        page.get_by_role("button", name="Dismiss twice").click()
        page.wait_for_function(
            "JSON.parse(localStorage.getItem('leakproof:checkout-events:browser-contract-session')"
            " || '[]').some(e => e.event_type === 'checkout_dismissed')"
        )
        page.reload()
        state["block_telemetry"] = False
        expect(page.get_by_text("Dismissal recorded", exact=False)).to_be_visible(timeout=12000)
        assert state["creates"] == 1
        assert len({value for value in dismissal_requests}) == 1
        assert len([e for e in events.values() if e["event_type"] == "checkout_dismissed"]) == 1
        checks.append(
            "selection, duplicate dismissals, offline queue and refresh retain one order/event"
        )
        capture("02-waiting-after-refresh")
        for phase, text in [
            ("provider_recheck", "Waiting for provider recheck"),
            ("provider_retry", "Provider recheck delayed"),
            ("provider_pending", "Payment pending at Razorpay"),
            ("confirmed", "Abandonment confirmed"),
        ]:
            state["phase"] = phase
            expect(page.get_by_text(text, exact=False)).to_be_visible(timeout=7000)
            capture("03-" + phase)
        checks.append("waiting, provider recheck, retry, pending and telemetry-confirmed states")
        page.get_by_role("link", name="Continue recovery").click()
        expect(page.get_by_role("button", name="Continue original order")).to_be_enabled(
            timeout=10000
        )
        initial_checks = state["bootstrap_checks"]
        page.get_by_role("button", name="Continue original order").click()
        expect(page.get_by_role("heading", name="Contract Checkout (fixture)")).to_be_visible()
        assert state["bootstrap_checks"] == initial_checks + 1
        assert page.evaluate("window.contractOrders.at(-1)") == session["razorpay_order_id"]
        page.get_by_role("button", name="Submit fixture").click()
        expect(page.get_by_text("Payment verified · recovery complete", exact=True)).to_be_visible(
            timeout=10000
        )
        assert state["verification_calls"] == 1
        expect(page.get_by_text("Cancelled", exact=True)).to_be_visible()
        capture("04-recovered-contract-only")
        page.reload()
        expect(page.get_by_text("Payment verified · recovery complete", exact=True)).to_be_visible()
        with page.expect_download() as download:
            page.get_by_role("button", name="Download acceptance evidence").click()
        download.value.save_as(args.output_dir / "downloaded-synthetic-acceptance.json")
        downloaded = json.loads(
            (args.output_dir / "downloaded-synthetic-acceptance.json").read_text()
        )
        assert downloaded["data_provenance"] == "SIMULATED_END_TO_END"
        checks.append(
            "order recheck, fixture verification, same-case closure and export on refresh"
        )
        page.get_by_role("button", name="Start a new demo").click()
        expect(page.get_by_role("button", name="Start checkout abandonment")).to_be_visible()
        assert state["creates"] == 1
        checks.append("new-demo navigation returns to scenario choice before creating an order")
        page.evaluate(
            "s => sessionStorage.setItem('leakproof:active-demo-session', JSON.stringify(s))",
            session,
        )
        state["phase"] = "payment_failure"
        page.goto(args.base_url + "/")
        expect(page.get_by_text("Payment failure takes precedence", exact=True)).to_be_visible()
        checks.append("failure precedence renders one existing case")
        # A recovery page that became stale must check again and never open Checkout.
        page.goto(args.base_url + "/recover/contract-token")
        expect(page.get_by_role("button", name="Continue original order")).to_be_enabled()
        state["bootstrap_blocked"] = True
        page.get_by_role("button", name="Continue original order").click()
        expect(page.get_by_text("Original order is already paid", exact=True)).to_be_visible()
        expect(page.get_by_role("heading", name="Contract Checkout (fixture)")).to_have_count(0)
        checks.append("stale recovery bootstrap fails closed before opening Checkout")
        state["expired"] = True
        page.goto(args.base_url + "/demo")
        expect(
            page.get_by_text(
                "Session expired. Recovery is disabled; start a new rehearsal.", exact=True
            )
        ).to_be_visible()
        expect(page.get_by_role("link", name="Continue recovery")).to_have_count(0)
        capture("05-expired")
        checks.append("session expiry clears storage and disables recovery")
        state["expired"] = False
        state["phase"] = "idle"
        page.get_by_role("button", name="Start a new rehearsal").click()
        expect(page.get_by_role("heading", name="Contract Checkout (fixture)")).to_be_visible()
        assert state["creates"] == 2
        checks.append("expired-session restart creates a new session rather than resuming")
        page.get_by_role("button", name="Dismiss twice").click()
        page.evaluate("sessionStorage.clear(); localStorage.clear()")
        state["phase"] = "idle"
        page.set_viewport_size({"width": 390, "height": 844})
        page.reload()
        expect(page.get_by_role("button", name="Start checkout abandonment")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        capture("06-mobile-entry")
        checks.append("mobile entry has no horizontal overflow")
        assert not errors, errors
        summary = {
            "passed": True,
            "evidence_kind": "BROWSER_CONTRACT_FIXTURES",
            "provider_rehearsal": "PENDING",
            "limitations": "API/SDK intercepted. No real provider, payment or email used.",
            "checks": checks,
            "page_errors": errors,
        }
        (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        browser.close()
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
