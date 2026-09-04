from __future__ import annotations

from sqlalchemy import func, select

from leakproof.batch import run_full_batch
from leakproof.measurement import compute_scoreboard, exception_report
from leakproof.models.db import Action, Event, RecoveryAttribution, RecoveryCase
from leakproof.simulator.generate import generate_dataset, load_parameters


def _small_parameters():
    parameters = load_parameters()
    return parameters.model_copy(
        update={
            "scale": parameters.scale.model_copy(
                update={"customers": 80, "b2b_invoice_customers": 10}
            ),
            "scenarios": parameters.scenarios.model_copy(
                update={
                    "issuer_outage": parameters.scenarios.issuer_outage.model_copy(
                        update={"failures": 20}
                    ),
                    "expired_card_cohort": parameters.scenarios.expired_card_cohort.model_copy(
                        update={"customers": 5}
                    ),
                    "merchant_misconfig": parameters.scenarios.merchant_misconfig.model_copy(
                        update={"failures": 2}
                    ),
                    "payday_clustering": parameters.scenarios.payday_clustering.model_copy(
                        update={"failures": 3}
                    ),
                    "invoice_aging": parameters.scenarios.invoice_aging.model_copy(
                        update={"overdue_invoices": 5}
                    ),
                }
            ),
            "breadth": parameters.breadth.model_copy(
                update={
                    "checkout_abandonment": 2,
                    "subscription_halt": 2,
                }
            ),
        }
    )


def test_full_batch_is_terminal_measured_audited_and_idempotent(session_factory):
    parameters = _small_parameters()
    dataset = generate_dataset(parameters)
    with session_factory() as session:
        first = run_full_batch(session, dataset, parameters)
        scoreboard = compute_scoreboard(session, dataset.run_id)
        exceptions = exception_report(session, dataset.run_id)

        assert first.cases_processed == len(dataset.signals)
        assert first.cases_suppressed == 20
        assert scoreboard.cases_processed == len(dataset.signals)
        assert scoreboard.suppressed_by_circuit_breaker == 20
        assert scoreboard.llm_cost_paise > 0
        assert scoreboard.false_chase_count == 0
        assert scoreboard.unresolved_exceptions == 0
        assert exceptions.total_cases == (
            len(dataset.signals)
            - session.scalar(select(func.count()).select_from(RecoveryAttribution))
        )
        assert {group.reason for group in exceptions.groups} >= {
            "COHORT_SUPPRESSION",
            "RECOVERY_NOT_OBSERVED",
        }
        assert all(
            case.outcome is not None
            for case in session.scalars(
                select(RecoveryCase).where(RecoveryCase.batch_run_id == dataset.run_id)
            )
        )

        counts_before = (
            session.scalar(select(func.count()).select_from(Event)),
            session.scalar(select(func.count()).select_from(Action)),
            session.scalar(select(func.count()).select_from(RecoveryAttribution)),
        )
        replay = run_full_batch(session, dataset, parameters)
        counts_after = (
            session.scalar(select(func.count()).select_from(Event)),
            session.scalar(select(func.count()).select_from(Action)),
            session.scalar(select(func.count()).select_from(RecoveryAttribution)),
        )

        assert replay.replayed is True
        assert counts_after == counts_before


def test_exception_endpoint_returns_every_non_recovered_case(
    session_factory, client
):
    parameters = _small_parameters()
    dataset = generate_dataset(parameters)
    with session_factory() as session:
        run_full_batch(session, dataset, parameters)
        expected = exception_report(session, dataset.run_id)

    response = client.get(f"/scoreboard/{dataset.run_id}/exceptions")

    assert response.status_code == 200
    assert response.json()["total_cases"] == expected.total_cases
    assert len(response.json()["items"]) == expected.total_cases
