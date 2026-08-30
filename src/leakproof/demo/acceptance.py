from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from leakproof.config import Settings
from leakproof.demo.contracts import (
    AcceptanceCaseSummary,
    AcceptanceCheck,
    AcceptanceProviderStatus,
    AcceptanceSessionSummary,
    DemoAcceptanceExport,
    DemoSessionState,
    TimelineItem,
)
from leakproof.demo.projection import get_demo_session_projection


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
            passed=email_action_exercised,
            severity="blocking",
            detail="The email step reached confirmed allowlisted delivery or preview-only.",
        ),
        AcceptanceCheck(
            check="original_order_recovered",
            passed=recovered,
            severity="blocking",
            detail="Webhook truth marked the original demo order as recovered.",
        ),
        AcceptanceCheck(
            check="same_case_closed",
            passed=closed,
            severity="blocking",
            detail="The detected case closed after the verified payment.",
        ),
        AcceptanceCheck(
            check="no_provider_failures",
            passed=projection.metrics.provider_failures == 0,
            severity="advisory",
            detail="Provider failures are recorded but do not invalidate the core recovery path.",
        ),
    ]

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
        exported_at=exported_at,
        passed=blocking_passed,
        session=AcceptanceSessionSummary(
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
