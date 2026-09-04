"""Invoice UI checks against intercepted contracts; no provider contact or payment."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from check_track_a_browser import WATERMARK
from playwright.sync_api import expect, sync_playwright


def scenario_capabilities():
    return [
        dict(
            scenario_type="PAYMENT_FAILURE",
            primary_entity_type="order",
            enabled=True,
            capability_evidence="LIVE_PROVIDER_VERIFIED",
            reason=None,
        ),
        dict(
            scenario_type="CHECKOUT_ABANDON",
            primary_entity_type="order",
            enabled=True,
            capability_evidence="LIVE_TELEMETRY_PROVIDER_RECONCILED",
            reason=None,
        ),
        dict(
            scenario_type="INVOICE_OVERDUE",
            primary_entity_type="invoice",
            enabled=True,
            capability_evidence="CONTRACT_VERIFIED",
            reason="Human hosted payment required.",
        ),
        dict(
            scenario_type="SUBSCRIPTION_HALT",
            primary_entity_type="subscription",
            enabled=True,
            capability_evidence="CONTRACT_VERIFIED",
            reason="Configured plan required.",
        ),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:3101")
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/track-b/browser"))
    args = parser.parse_args()
    if urlparse(args.base_url).hostname not in {"127.0.0.1", "localhost"}:
        parser.error("Use an isolated loopback dashboard")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    created = dict(
        session_id="invoice-browser-fixture",
        session_token="fixture-session-token",
        primary_entity_type="invoice",
        primary_entity_id="inv_fixture",
        scenario_type="INVOICE_OVERDUE",
        setup_state="READY",
        amount_paise=50000,
        currency="INR",
        email_mode="preview_only",
        expires_at=(now + timedelta(minutes=30)).isoformat(),
    )
    state = dict(
        phase="waiting", bootstrap="payable", creates=0, checks=0, hosted=0, sdk=0, error=0
    )
    errors, checks = [], []

    def projection():
        phase = state["phase"]
        recovered = phase == "paid"
        risk = phase != "waiting"
        outstanding = 0 if recovered else 30000 if phase == "partial" else 50000
        decision = (
            "merchant_review"
            if phase in {"expired", "cancelled"}
            else "provider_retry"
            if phase == "retry"
            else "paid"
            if recovered
            else "payable"
        )
        invoice = dict(
            provider_status="partially_paid"
            if phase == "partial"
            else "issued"
            if phase in {"waiting", "overdue", "retry"}
            else phase,
            business_due_at=(
                now + timedelta(seconds=60) if not risk else now - timedelta(seconds=60)
            ).isoformat(),
            business_overdue=risk and not recovered,
            aging_bucket="under_1_day" if risk else "not_due",
            provider_expires_at=(now + timedelta(minutes=20)).isoformat(),
            detected_balance_paise=50000 if risk else None,
            outstanding_balance_paise=outstanding,
            amount_paid_paise=50000 - outstanding,
            recovered_paise=50000 - outstanding,
            disposition=decision,
            last_checked_at=now.isoformat(),
            partial_payment=True,
        )
        metrics = dict(
            cases_detected=int(risk),
            recovered_cases=int(recovered),
            recovered_amount_paise=50000 - outstanding,
            recovery_rate=int(recovered),
            median_recovery_time_seconds=100 if recovered else None,
            provider_failures=int(phase == "retry"),
            luna_cost_paise=0,
        )
        return dict(
            **created,
            data_provenance="SIMULATED_END_TO_END",
            capability_evidence="CONTRACT_VERIFIED",
            invoice=invoice,
            state="RECOVERED" if recovered else "AT_RISK" if risk else "CREATED",
            case=dict(
                case_id="fixture-case",
                leak_type="INVOICE_OVERDUE",
                state="CLOSED" if recovered else "DIAGNOSED",
                deterministic_diagnosis={
                    "rule_id": "fixture",
                    "failure_class": "UNKNOWN",
                    "confidence": 1,
                },
                insight=None,
                insight_status="pending",
            )
            if risk
            else None,
            abandonment_check=dict(
                status="idle", due_at=None, browser_dismissed_at=None, unpaid_confirmed=False
            ),
            recovery_url_available=risk and decision == "payable",
            recovery_path="/recover/invoice-fixture-token"
            if risk and decision == "payable"
            else None,
            gate_verdict=None,
            recovery_actions=[
                dict(
                    action_type="merchant_review"
                    if decision == "merchant_review"
                    else "invoice_payment_link",
                    status="completed" if recovered else "available",
                    scheduled_for=now.isoformat(),
                    executed_at=None,
                    gate_verdict=None,
                    provider_receipt_id=None,
                    action_id=None,
                )
            ]
            if risk
            else [],
            provider_statuses=[],
            timeline=[],
            end_to_end_latency_seconds=None,
            metrics=metrics,
            environment_metrics=metrics,
        )

    def route_handler(route):
        path = urlparse(route.request.url).path
        host = urlparse(route.request.url).hostname
        if host == "checkout.razorpay.com":
            state["sdk"] += 1
            return route.abort()
        if host == "rzp.io":
            state["hosted"] += 1
            return route.fulfill(
                content_type="text/html", body="<h1>Intercepted original invoice fixture</h1>"
            )
        if host not in {"127.0.0.1", "localhost"}:
            return route.abort()

        def reply(body, status=200):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

        if path == "/api/demo/scenarios":
            return reply(scenario_capabilities())
        if path == "/api/demo/sessions" and route.request.method == "POST":
            assert route.request.post_data_json["scenario_type"] == "INVOICE_OVERDUE"
            state["creates"] += 1
            return reply(created, 201)
        if path.endswith("/acceptance.json"):
            assert route.request.headers["x-leakproof-session-token"] == created["session_token"]
            return reply(
                json.loads(Path("artifacts/track-b/contract/invoice-partial-full.json").read_text())
            )
        if path.startswith("/api/demo/sessions/"):
            assert "checkout-events" not in path and "payments/verify" not in path
            assert route.request.headers["x-leakproof-session-token"] == created["session_token"]
            return reply(projection())
        if path.startswith("/api/recover/"):
            state["checks"] += 1
            if state["error"]:
                code, message = {
                    404: ("invalid_recovery_token", "recovery link is invalid"),
                    410: ("recovery_expired", "recovery link has expired"),
                    503: ("timeout", "Provider verification unavailable"),
                }[state["error"]]
                return reply(
                    {
                        "error": {
                            "code": code,
                            "message": message,
                            "retryable": state["error"] == 503,
                        }
                    },
                    state["error"],
                )
            return reply(
                dict(
                    purpose="invoice_hosted_payment",
                    session_id=created["session_id"],
                    disposition=state["bootstrap"],
                    redirect_url="https://rzp.io/i/fixture"
                    if state["bootstrap"] == "payable"
                    else None,
                    amount_due_paise=30000 if state["bootstrap"] != "paid" else 0,
                    currency="INR",
                    expires_at=created["expires_at"],
                )
            )
        if path.startswith("/api/"):
            return reply([])
        return route.continue_()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1440, "height": 1100}, accept_downloads=True
        )
        context.add_init_script(WATERMARK)
        context.route("**/*", route_handler)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.base_url + "/demo")
        expect(page.get_by_role("group", name="Choose one provider rehearsal")).to_be_visible()
        expect(page.get_by_role("radio")).to_have_count(4)
        for available in ("Payment failure", "Checkout abandonment", "Subscription halt"):
            page.get_by_role("radio", name=available, exact=True).check()
            expect(page.get_by_role("radio", name=available, exact=True)).to_be_checked()
        checks.append("four_capability_cards_and_all_available_scenario_controls")
        page.get_by_label("Invoice overdue", exact=True).check()
        page.get_by_role("button", name="Create test invoice").click()
        expect(page.get_by_role("region", name="Invoice balance and status")).to_be_visible()
        expect(page.get_by_text("Not overdue", exact=False)).to_be_visible()
        assert state["creates"] == 1 and state["sdk"] == 0
        checks.append("invoice_selection_setup_without_checkout_sdk")
        page.reload()
        expect(page.get_by_text("Awaiting detection", exact=True)).to_be_visible()
        assert state["creates"] == 1
        checks.append("session_refresh_reuses_original_invoice")
        state["phase"] = "partial"
        expect(page.get_by_text("Partial payment received.", exact=False)).to_be_visible(
            timeout=10000
        )
        status = page.get_by_role("region", name="Invoice balance and status")
        expect(status.get_by_text("₹500", exact=True)).to_be_visible()
        expect(status.get_by_text("₹300", exact=True)).to_be_visible()
        page.screenshot(path=str(args.output_dir / "partial-desktop.png"), full_page=True)
        checks.append("partial_keeps_original_detected_balance_and_current_outstanding")
        page.get_by_role("link", name="Continue recovery").click()
        expect(page.get_by_role("button", name="Continue original invoice")).to_be_enabled()
        before = state["checks"]
        state["bootstrap"] = "merchant_review"
        page.get_by_role("button", name="Continue original invoice").click()
        expect(page.get_by_text("Payment is unavailable.", exact=False)).to_be_visible()
        expect(page.get_by_role("button", name="Continue original invoice")).to_have_count(0)
        assert state["checks"] == before + 1 and state["hosted"] == 0
        checks.append("stale_payable_bootstrap_rechecked_and_expired_cta_removed")
        state["bootstrap"] = "payable"
        page.reload()
        page.get_by_role("button", name="Continue original invoice").click()
        expect(
            page.get_by_role("heading", name="Intercepted original invoice fixture")
        ).to_be_visible()
        assert state["hosted"] == 1 and state["sdk"] == 0
        checks.append("original_invoice_hosted_navigation_only_after_recheck")
        for phase in ["expired", "cancelled", "retry"]:
            state["phase"] = phase
            page.goto(args.base_url)
            expect(page.get_by_role("region", name="Invoice balance and status")).to_be_visible()
            expect(page.get_by_role("link", name="Continue recovery")).to_have_count(0)
        checks.append("expired_cancelled_and_provider_failure_hold_recovery")
        page.set_viewport_size({"width": 390, "height": 844})
        state["phase"] = "cancelled"
        page.reload()
        expect(page.get_by_text("Payment is unavailable.", exact=False)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=str(args.output_dir / "merchant-review-mobile.png"), full_page=True)
        checks.append("mobile_merchant_review_without_horizontal_overflow")
        for code in [404, 410, 503]:
            state["error"] = code
            page.goto(args.base_url + "/recover/invoice-fixture-token")
            expect(page.get_by_role("button", name="Check the link again")).to_be_visible()
            expect(page.get_by_role("button", name="Continue original order")).to_be_disabled()
        checks.append("invalid_expired_tokens_and_retryable_provider_error")
        state["error"], state["phase"], state["bootstrap"] = 0, "paid", "paid"
        page.goto(args.base_url + "/recover/invoice-fixture-token")
        expect(page.get_by_text("This invoice is paid.", exact=False)).to_be_visible()
        expect(page.get_by_role("button", name="Continue original invoice")).to_have_count(0)
        page.goto(args.base_url)
        expect(page.get_by_text("Invoice settled.", exact=False)).to_be_visible()
        with page.expect_download() as download:
            page.get_by_role("button", name="Download acceptance evidence").click()
        exported = json.loads(Path(download.value.path()).read_text())
        assert exported["session"]["scenario_type"] == "INVOICE_OVERDUE"
        assert exported["data_provenance"] == "SIMULATED_END_TO_END" and exported["passed"]
        checks.append("paid_case_no_cta_and_sanitized_acceptance_download")
        assert not errors, errors
        browser.close()
    summary = dict(
        passed=True,
        groups=checks,
        page_errors=errors,
        evidence="SIMULATED_END_TO_END",
        provider_rehearsal="PENDING: human test payment",
        external_provider_requests=0,
    )
    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
