from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from leakproof.audit.timeline import replay_case
from leakproof.config import Settings
from leakproof.demo.contracts import (
    AcceptanceCaseSummary,
    AcceptanceCheck,
    AcceptanceProviderStatus,
    AcceptanceSessionSummary,
    DemoAcceptanceExport,
    DemoSessionState,
    TimelineItem,
    live_case_dedupe_key,
)
from leakproof.demo.projection import get_demo_session_projection
from leakproof.models.db import DemoSession, ProviderCall, RecoveryCase, WebhookEvent


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def build_demo_acceptance_export(
    session: Session,
    session_id: str,
    *,
    session_token: str,
    settings: Settings,
    now: datetime | None = None,
) -> DemoAcceptanceExport:
    """Build a release artifact without tokens, provider IDs, or browser identifiers."""
    exported_at = _utc(now or datetime.now(UTC))
    projection = get_demo_session_projection(
        session,
        session_id,
        session_token=session_token,
        settings=settings,
        now=exported_at,
    )
    case = projection.case
    demo = session.get(DemoSession, session_id)
    case_row = session.get(RecoveryCase, case.case_id) if case is not None else None
    recovery_action_registered = any(
        action.action_type == "recovery_link" for action in projection.recovery_actions
    )
    email_preview_completed = any(
        action.action_type == "email_link" and action.status == "preview_only"
        for action in projection.recovery_actions
    )
    allowlisted_delivery_confirmed = any(
        item.provider == "resend"
        and item.operation == "delivery"
        and item.status in {"delivered", "clicked"}
        for item in projection.provider_statuses
    )
    email_action_exercised = email_preview_completed or allowlisted_delivery_confirmed
    insight_resolved = case is not None and case.insight_status in {"succeeded", "fallback"}
    recovered = projection.state == DemoSessionState.RECOVERED
    closed = case is not None and case.state == "CLOSED"
    original_order_reused = (
        demo is not None
        and case_row is not None
        and case_row.dedupe_key == live_case_dedupe_key(demo.id, demo.razorpay_order_id)
    )
    success_webhooks = list(
        session.scalars(
            select(WebhookEvent).where(
                WebhookEvent.merchant_id == (demo.merchant_id if demo is not None else ""),
                WebhookEvent.provider == "razorpay",
                WebhookEvent.event_type.in_(["payment.captured", "order.paid"]),
                WebhookEvent.processed_at.is_not(None),
            )
        )
    )
    webhook_verified = (
        recovered
        and demo is not None
        and any(
            (
                item.event_type == "payment.captured"
                and ((item.payload.get("payload") or {}).get("payment") or {})
                .get("entity", {})
                .get("order_id")
                == demo.razorpay_order_id
            )
            or (
                item.event_type == "order.paid"
                and ((item.payload.get("payload") or {}).get("order") or {})
                .get("entity", {})
                .get("id")
                == demo.razorpay_order_id
            )
            for item in success_webhooks
        )
    )
    checkout_verified = (
        recovered
        and demo is not None
        and session.scalar(
            select(ProviderCall.id).where(
                ProviderCall.session_id == demo.id,
                ProviderCall.provider == "razorpay",
                ProviderCall.operation == "verify_checkout_payment",
                ProviderCall.status == "succeeded",
            )
        )
        is not None
    )
    api_capture_verified = recovered and any(
        "captured" in call.safe_response_metadata.get("statuses", [])
        for call in session.scalars(
            select(ProviderCall).where(
                ProviderCall.session_id == session_id,
                ProviderCall.provider == "razorpay",
                ProviderCall.operation.in_(["list_order_payments", "recovery_order_check"]),
                ProviderCall.status == "succeeded",
            )
        )
    )
    provider_verified = webhook_verified or checkout_verified or api_capture_verified
    no_pending_contacts = not any(
        action.action_type == "email_link" and action.status == "pending"
        for action in projection.recovery_actions
    )
    recovered_amount_matches = (
        recovered
        and projection.metrics.recovered_cases == 1
        and projection.metrics.recovered_amount_paise == projection.amount_paise
    )
    replay_matches = False
    if case_row is not None:
        replay_matches = replay_case(session, case_row.id).projection_matches
    latest_payment_calls = {
        item.operation: item for item in projection.provider_statuses if item.provider == "razorpay"
    }
    blocking_provider_failure = any(
        item.status == "failed" for item in latest_payment_calls.values()
    )
    checks = [
        AcceptanceCheck(
            check="case_detected",
            passed=case is not None,
            severity="blocking",
            detail="One live recovery case is attached to the session.",
        ),
        AcceptanceCheck(
            check="deterministic_diagnosis_ready",
            passed=case is not None and case.deterministic_diagnosis is not None,
            severity="blocking",
            detail="Tier 1 diagnosis remains the authoritative decision.",
        ),
        AcceptanceCheck(
            check="insight_or_fallback_ready",
            passed=insight_resolved,
            severity="blocking",
            detail="Luna insight or deterministic fallback completed without blocking recovery.",
        ),
        AcceptanceCheck(
            check="recovery_action_registered",
            passed=recovery_action_registered,
            severity="blocking",
            detail="The original-order recovery action was registered.",
        ),
        AcceptanceCheck(
            check="email_action_exercised",
            passed=email_action_exercised
            or (
                recovered
                and any(
                    action.action_type == "email_link" and action.status == "cancelled"
                    for action in projection.recovery_actions
                )
            ),
            severity="blocking",
            detail="Email reached delivery, preview, or cancellation after verified recovery.",
        ),
        AcceptanceCheck(
            check="original_order_recovered",
            passed=recovered,
            severity="blocking",
            detail="Server-verified Razorpay truth marked the original demo order as recovered.",
        ),
        AcceptanceCheck(
            check="original_order_reused",
            passed=original_order_reused,
            severity="blocking",
            detail="The recovery stayed bound to the order that created the case.",
        ),
        AcceptanceCheck(
            check="provider_verified_payment",
            passed=provider_verified,
            severity="blocking",
            detail=(
                "A signed webhook or signed Checkout result plus captured-payment API check "
                "entered the verification timeline."
            ),
        ),
        AcceptanceCheck(
            check="same_case_closed",
            passed=closed,
            severity="blocking",
            detail="The detected case closed after the verified payment.",
        ),
        AcceptanceCheck(
            check="pending_contacts_cancelled",
            passed=no_pending_contacts,
            severity="blocking",
            detail="No pending customer-contact action remains after recovery.",
        ),
        AcceptanceCheck(
            check="session_recovered_amount_correct",
            passed=recovered_amount_matches,
            severity="blocking",
            detail="Current-session recovery metrics contain exactly this order amount.",
        ),
        AcceptanceCheck(
            check="audit_projection_replay_matches",
            passed=replay_matches,
            severity="blocking",
            detail="The append-only event replay matches the stored case projection.",
        ),
        AcceptanceCheck(
            check="no_blocking_provider_failure",
            passed=not blocking_provider_failure,
            severity="blocking",
            detail="No Razorpay provider failure blocks order creation or verification.",
        ),
        AcceptanceCheck(
            check="no_provider_failures",
            passed=projection.metrics.provider_failures == 0,
            severity="advisory",
            detail="Provider failures are recorded but do not invalidate the core recovery path.",
        ),
    ]

    if projection.scenario_type == "CHECKOUT_ABANDON" or (
        case and case.leak_type == "CHECKOUT_ABANDON"
    ):
        checks.extend(
            [
                AcceptanceCheck(
                    check="browser_dismissal_recorded",
                    severity="blocking",
                    passed=any(
                        item.kind == "checkout_dismissed" and item.source == "browser"
                        for item in projection.timeline
                    ),
                    detail="Browser telemetry records dismissal independently of payment truth.",
                ),
                AcceptanceCheck(
                    check="unpaid_order_rechecked",
                    severity="blocking",
                    passed=projection.abandonment_check.unpaid_confirmed,
                    detail="Razorpay confirmed the dismissed order was unpaid.",
                ),
                AcceptanceCheck(
                    check="original_order_reopened",
                    severity="blocking",
                    passed=any(
                        item.operation == "recovery_order_check" and item.status == "succeeded"
                        for item in projection.provider_statuses
                    ),
                    detail="The signed recovery route rechecked the original order.",
                ),
            ]
        )

    # Browser metadata can contain per-attempt identifiers. The acceptance artifact needs the
    # event sequence and safe classifications, not those identifiers.
    timeline = [
        TimelineItem(
            kind=item.kind,
            source=item.source,
            occurred_at=item.occurred_at,
            payload={} if item.source == "browser" else item.payload,
        )
        for item in projection.timeline
    ]
    blocking_passed = all(item.passed for item in checks if item.severity == "blocking")
    return DemoAcceptanceExport(
        data_provenance=projection.data_provenance,
        exported_at=exported_at,
        passed=blocking_passed,
        session=AcceptanceSessionSummary(
            scenario_type=projection.scenario_type,
            state=projection.state,
            amount_paise=projection.amount_paise,
            currency=projection.currency,
            email_mode=projection.email_mode,
        ),
        case=(
            AcceptanceCaseSummary(
                leak_type=case.leak_type,
                state=case.state,
                deterministic_diagnosis_ready=case.deterministic_diagnosis is not None,
                insight_status=case.insight_status,
            )
            if case is not None
            else None
        ),
        operational_metrics=projection.metrics,
        provider_statuses=[
            AcceptanceProviderStatus(
                provider=item.provider,
                operation=item.operation,
                status=item.status,
                latency_ms=item.latency_ms,
                attempts=item.attempts,
                error_class=item.error_class,
            )
            for item in projection.provider_statuses
        ],
        timeline=timeline,
        checks=checks,
    )
