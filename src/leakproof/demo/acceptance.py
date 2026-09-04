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
    if demo.primary_entity_type == "invoice":
        return _invoice_acceptance(session, demo, projection, exported_at)
    if demo.primary_entity_type == "subscription":
        return _subscription_acceptance(session, demo, projection, exported_at)
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


def _invoice_acceptance(session, demo, projection, exported_at):
    from leakproof.models.db import Event, ProviderObligation, Settlement

    obligation = session.scalar(
        select(ProviderObligation).where(
            ProviderObligation.merchant_id == demo.merchant_id,
            ProviderObligation.provider_entity_id == demo.primary_entity_id,
            ProviderObligation.mode == "test",
        )
    )
    case = projection.case
    events = list(
        session.scalars(
            select(Event).where(Event.case_id == (case.case_id if case else "__none__"))
        )
    )
    settlements = list(
        session.scalars(select(Settlement).where(Settlement.obligation_id == obligation.id))
    )
    settlement_ids = {item.payment_id for item in settlements}
    globally_matching_settlements = list(
        session.scalars(
            select(Settlement).where(
                Settlement.merchant_id == demo.merchant_id,
                Settlement.provider == "razorpay",
                Settlement.mode == demo.provider_mode,
                Settlement.payment_id.in_(settlement_ids),
            )
        )
    ) if settlement_ids else []
    latest = {p.operation: p for p in projection.provider_statuses if p.provider == "razorpay"}
    invoice = projection.invoice
    review = bool(invoice and invoice.disposition == "merchant_review")
    partial = any(
        e.kind == "INVOICE_RECONCILED"
        and e.payload.get("provider_status") == "partially_paid"
        and e.payload.get("amount_due_paise", 0) > 0
        and e.payload.get("case_open") is True
        for e in events
    )
    checks = []

    def check(name, passed, detail, severity="blocking"):
        checks.append(
            AcceptanceCheck(check=name, passed=bool(passed), severity=severity, detail=detail)
        )

    check("case_detected", case, "One provider-correlated invoice case was detected.")
    check(
        "original_invoice_reused",
        case and obligation.case_id == case.case_id,
        "The case remains attached to its original invoice obligation.",
    )
    check(
        "invoice_due_policy_recorded",
        invoice and invoice.business_due_at,
        "Application business due date is recorded separately from provider expiry.",
    )
    check(
        "invoice_payment_ledger_unique",
        len({p.payment_id for p in settlements}) == len(settlements),
        "Each captured payment appears once in the merchant-scoped ledger.",
    )
    check(
        "captured_payment_globally_unique",
        len(globally_matching_settlements) == len(settlement_ids),
        "No captured payment is counted by another order, invoice, or subscription surface.",
    )
    check(
        "audit_projection_replay_matches",
        case and replay_case(session, case.case_id).projection_matches,
        "Append-only audit replay matches the case projection.",
    )
    check(
        "no_blocking_provider_failure",
        latest and all(p.status != "failed" for p in latest.values()),
        "Latest provider setup and reconciliation calls succeeded.",
    )
    if review:
        check(
            "nonpayable_invoice_has_no_payment_cta",
            not projection.recovery_url_available,
            "Non-payable invoice is routed to merchant review without a payment CTA.",
        )
        check(
            "nonpayable_invoice_not_recovered",
            projection.state != "RECOVERED",
            "Expiry or cancellation does not establish payment.",
        )
    else:
        check(
            "invoice_partial_payment_kept_open",
            partial,
            "A reconciled partial payment retained the original open case.",
        )
        check(
            "original_invoice_opened",
            "recovery_invoice_check" in latest,
            "The bound recovery route rechecked the original hosted invoice.",
        )
        check(
            "same_case_closed",
            case and case.state == "CLOSED" and projection.state == "RECOVERED",
            "Verified full invoice settlement closed the detected case.",
        )
        check(
            "session_recovered_amount_correct",
            invoice
            and invoice.outstanding_balance_paise == 0
            and obligation.recovered_paise == obligation.detected_due_paise
            and sum(p.credited_paise for p in settlements) == obligation.detected_due_paise,
            "Incremental credit equals the original detected unpaid balance.",
        )
        check(
            "provider_verified_payment",
            invoice
            and invoice.provider_status == "paid"
            and sum(p.amount_paise for p in settlements) == demo.amount_paise,
            "Invoice paid state is backed by unique captured payment identities.",
        )
    check(
        "pending_contacts_cancelled",
        not any(
            a.status == "pending"
            for a in projection.recovery_actions
            if a.action_type == "email_link"
        ),
        "No pending customer contact remains after settlement or merchant review.",
    )
    check(
        "optional_email_exercised",
        any(a.action_type == "email_link" for a in projection.recovery_actions),
        "Optional allowlisted email or preview was scheduled.",
        "advisory",
    )
    return DemoAcceptanceExport(
        data_provenance=projection.data_provenance,
        exported_at=exported_at,
        passed=all(c.passed for c in checks if c.severity == "blocking"),
        invoice=invoice,
        session=AcceptanceSessionSummary(
            scenario_type=demo.scenario_type,
            state=projection.state,
            amount_paise=demo.amount_paise,
            currency=demo.currency,
            email_mode=projection.email_mode,
        ),
        case=AcceptanceCaseSummary(
            leak_type=case.leak_type,
            state=case.state,
            deterministic_diagnosis_ready=case.deterministic_diagnosis is not None,
            insight_status=case.insight_status,
        )
        if case
        else None,
        operational_metrics=projection.metrics,
        provider_statuses=[
            AcceptanceProviderStatus(**p.model_dump(exclude={"request_id"}))
            for p in projection.provider_statuses
        ],
        timeline=projection.timeline,
        checks=checks,
    )


def _subscription_acceptance(session, demo, projection, exported_at):
    from leakproof.models.db import Event, ProviderEntity, ProviderObligation, Settlement

    parent = session.scalar(
        select(ProviderEntity).where(
            ProviderEntity.merchant_id == demo.merchant_id,
            ProviderEntity.mode == demo.provider_mode,
            ProviderEntity.entity_type == "subscription",
            ProviderEntity.provider_entity_id == demo.primary_entity_id,
        )
    )
    cycle_id = (parent.safe_metadata or {}).get("affected_invoice_id") if parent else None
    obligation = (
        session.scalar(
            select(ProviderObligation).where(
                ProviderObligation.merchant_id == demo.merchant_id,
                ProviderObligation.mode == demo.provider_mode,
                ProviderObligation.entity_type == "invoice",
                ProviderObligation.provider_entity_id == cycle_id,
            )
        )
        if cycle_id
        else None
    )
    case = projection.case
    events = list(
        session.scalars(
            select(Event).where(Event.case_id == (case.case_id if case else "__none__"))
        )
    )
    settlements = list(
        session.scalars(
            select(Settlement).where(
                Settlement.obligation_id == (obligation.id if obligation else "__none__")
            )
        )
    )
    settlement_ids = {item.payment_id for item in settlements}
    globally_matching_settlements = list(
        session.scalars(
            select(Settlement).where(
                Settlement.merchant_id == demo.merchant_id,
                Settlement.provider == "razorpay",
                Settlement.mode == demo.provider_mode,
                Settlement.payment_id.in_(settlement_ids),
            )
        )
    ) if settlement_ids else []
    latest = {p.operation: p for p in projection.provider_statuses if p.provider == "razorpay"}
    subscription = projection.subscription
    reconciliations = [e for e in events if e.kind == "SUBSCRIPTION_RECONCILED"]
    statuses = [e.payload.get("subscription_status") for e in reconciliations]
    checks = []

    def check(name, passed, detail, severity="blocking"):
        checks.append(
            AcceptanceCheck(check=name, passed=bool(passed), severity=severity, detail=detail)
        )

    check(
        "case_detected", case and obligation, "One exact subscription-invoice cycle owns the case."
    )
    check(
        "pending_to_halted_same_case",
        "pending" in statuses
        and "halted" in statuses
        and obligation
        and obligation.case_id == case.case_id,
        "Pending and halted observations escalated one cycle case.",
    )
    check(
        "razorpay_owns_retries",
        subscription and subscription.retry_owner == "razorpay",
        "Recurring retries remain provider-owned.",
    )
    check(
        "no_app_owned_debit",
        all(
            p.operation not in {"charge_subscription", "retry_subscription", "resume_subscription"}
            for p in projection.provider_statuses
        ),
        "Leakproof issued no debit, retry, or resume operation.",
    )
    check(
        "method_update_rechecked",
        "recovery_subscription_check" in latest,
        "The method-update route rechecked current provider state and exact arrears.",
    )
    check(
        "cycle_payment_ledger_unique",
        len({p.payment_id for p in settlements}) == len(settlements),
        "Captured payments are unique within the exact invoice obligation.",
    )
    check(
        "captured_payment_globally_unique",
        len(globally_matching_settlements) == len(settlement_ids),
        "No captured payment is counted by another order, invoice, or subscription surface.",
    )
    check(
        "audit_projection_replay_matches",
        case and replay_case(session, case.case_id).projection_matches,
        "Append-only replay matches the stored case.",
    )
    check(
        "no_blocking_provider_failure",
        latest and all(p.status != "failed" for p in latest.values()),
        "Latest provider setup and reconciliation calls succeeded.",
    )
    if projection.state == DemoSessionState.RECOVERED:
        check(
            "exact_invoice_settled",
            subscription
            and subscription.outstanding_balance_paise == 0
            and obligation
            and obligation.settled_at,
            "The affected invoice—not activation alone—was fully settled.",
        )
        check(
            "same_case_closed",
            case and case.state == "CLOSED",
            "Settlement closed the same billing-cycle case.",
        )
        check(
            "recovered_revenue_is_captured",
            obligation
            and obligation.recovered_paise == obligation.detected_due_paise
            and sum(p.credited_paise for p in settlements) == obligation.detected_due_paise,
            "Recovered revenue is backed by captured payment identities.",
        )
    else:
        check(
            "activation_not_counted_as_revenue",
            not subscription
            or subscription.provider_status != "active"
            or projection.metrics.recovered_amount_paise == 0,
            "Service activation alone did not count recovered revenue.",
        )
    check(
        "intentional_states_have_no_cta",
        not subscription
        or subscription.provider_status not in {"paused", "cancelled", "completed", "expired"}
        or not projection.recovery_url_available,
        "Intentional and terminal states cannot update or restart the subscription.",
    )
    check(
        "optional_email_exercised",
        any(a.action_type == "email_link" for a in projection.recovery_actions),
        "Optional allowlisted email or preview was scheduled.",
        "advisory",
    )

    return DemoAcceptanceExport(
        data_provenance=projection.data_provenance,
        exported_at=exported_at,
        passed=all(c.passed for c in checks if c.severity == "blocking"),
        subscription=subscription,
        session=AcceptanceSessionSummary(
            scenario_type=demo.scenario_type,
            state=projection.state,
            amount_paise=demo.amount_paise,
            currency=demo.currency,
            email_mode=projection.email_mode,
        ),
        case=AcceptanceCaseSummary(
            leak_type=case.leak_type,
            state=case.state,
            deterministic_diagnosis_ready=case.deterministic_diagnosis is not None,
            insight_status=case.insight_status,
        )
        if case
        else None,
        operational_metrics=projection.metrics,
        provider_statuses=[
            AcceptanceProviderStatus(**p.model_dump(exclude={"request_id"}))
            for p in projection.provider_statuses
        ],
        timeline=projection.timeline,
        checks=checks,
    )
