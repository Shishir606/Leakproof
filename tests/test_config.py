from leakproof.config import get_measurement_config, get_policy_config, get_settings


def test_all_foundation_configuration_is_typed_and_loads():
    config = get_policy_config()

    assert {action.key for action in config.actions} >= {
        "silent_retry",
        "whatsapp_link",
        "human_handoff",
    }
    assert config.templates[0].variables == [
        "payer_name",
        "invoice_no",
        "amount",
        "due_date",
        "link",
    ]
    assert len(config.guardrails.stopping_rules) == 7
    assert config.guardrails.schedule.contact_window_ist.start.hour == 8
    assert config.models.budgets.per_batch_paise == 50_000
    assert config.policy_defaults.margin == 1.0
    assert config.policy_defaults.annoyance_lambda == 0.02
    assert {ladder.leak_type for ladder in config.ladders} == {
        "PAYMENT_FAILURE",
        "CHECKOUT_ABANDON",
        "SUBSCRIPTION_HALT",
        "INVOICE_OVERDUE",
    }
    measurement = get_measurement_config()
    assert measurement.holdout.fraction == 0.10
    assert measurement.holdout.seed == 42
    assert measurement.holdout.stratify_by == ["leak_type", "amount_band"]
    assert measurement.attribution.windows_days["PAYMENT_FAILURE"] == 7
    assert measurement.attribution.windows_days["INVOICE_OVERDUE"] == 21
    assert measurement.economics.contribution_margin_rate == 0.68
    assert measurement.economics.human_review_unit_cost_paise == 3500
    assert measurement.economics.excluded_costs
    assert measurement.uncertainty.confidence_level == 0.80


def test_simulation_remains_the_safe_default():
    assert get_settings().mode == "simulation"
