export type ArmMetrics = {
  eligible_cases: number;
  recovered_cases: number;
  amount_at_risk_paise: number;
  recovered_paise: number;
  recovery_rate: number;
};

export type Scoreboard = {
  run_id: string;
  data_provenance: "LIVE_PROVIDER_VERIFIED" | "SIMULATED_END_TO_END" | "ARCHITECTURE_READY";
  merchant_id: string;
  synthetic: boolean;
  started_at: string;
  completed_at: string;
  duration_seconds: number;
  cases_processed: number;
  cases_by_leak_type: Record<string, number>;
  throughput_cases_per_minute: number;
  treatment: ArmMetrics;
  holdout: ArmMetrics;
  lift_percentage_points: number;
  gross_recovered_paise: number;
  organic_holdout_paise: number;
  counterfactual_organic_paise: number;
  incremental_recovered_paise: number;
  incremental_revenue_paise: number;
  contribution_margin_paise: number;
  intervention_cost_paise: number;
  llm_cost_paise: number;
  human_review_cost_paise: number;
  included_optional_cost_paise: number;
  net_economic_value_paise: number;
  net_value_created_paise: number;
  contacts: number;
  contacts_per_1000_rupees_recovered: number;
  opt_out_rate: number;
  false_chase_count: number;
  suppressed_by_circuit_breaker: number;
  declined_ev_non_positive: number;
  escalated_to_human: number;
  unresolved_exceptions: number;
  estimator: string;
  assumption_hash: string;
  seed_count: number;
  assumptions: {
    contribution_margin_rate: number;
    human_review_unit_cost_paise: number;
    included_optional_costs_paise_per_case: Record<string, number>;
    excluded_costs: string[];
    holdout_fraction: number;
    attribution_windows_days: Record<string, number>;
    intervention_effects: Record<string, unknown>;
  };
  uncertainty: {
    confidence_level: number;
    method: string;
    lift_percentage_points: EstimateInterval;
    incremental_revenue_paise: EstimateInterval;
    contribution_margin_paise: EstimateInterval;
    net_economic_value_paise: EstimateInterval;
  };
};

export type EstimateInterval = {
  median: number;
  minimum: number;
  maximum: number;
  interval_low: number;
  interval_high: number;
};

export type CaseListItem = {
  id: string;
  batch_run_id: string | null;
  customer_id: string;
  leak_type: string;
  entity_type: string;
  entity_id: string;
  amount_at_risk: number;
  currency: string;
  state: string;
  arm: string;
  outcome: string | null;
  detected_at: string;
  closed_at: string | null;
  event_count: number;
};

export type CaseList = {
  items: CaseListItem[];
  total: number;
  limit: number;
  offset: number;
};

export type CaseEvent = {
  seq: number;
  kind: string;
  payload: Record<string, unknown>;
  actor: string;
  occurred_at: string;
};

export type CaseDetail = {
  case: CaseListItem;
  replay: {
    case: Omit<CaseListItem, "closed_at" | "event_count" | "outcome"> & {
      merchant_id: string;
      dedupe_key: string;
      amount_band: string;
      attribution_until: string;
    };
    events: CaseEvent[];
    replayed_state: string;
    projection_matches: boolean;
  };
  diagnosis: null | {
    tier: number;
    failure_class: string;
    confidence: number;
    evidence: Record<string, unknown>;
    rule_id: string | null;
    diagnosed_at: string;
  };
  actions: Array<{
    id: string;
    step_index: number;
    action_type: string;
    scheduled_for: string;
    verdict: string | null;
    verdict_rules: null | { rules?: Array<Record<string, unknown>> };
    executed_at: string | null;
    provider_ref: string | null;
    status: string | null;
    attempt_count: number;
    cost_paise: number;
    ev_estimate: number | null;
  }>;
  attribution: null | {
    amount_paise: number;
    matched_by: string;
    credit_rule: string;
    credited_action_type: string | null;
    organic: boolean;
    paid_at: string;
  };
};

export type LatestEvals = {
  overall_passed: boolean;
  runs: Array<{
    id: number;
    suite: string;
    prompt_version: string | null;
    model: string | null;
    metrics: Record<string, unknown>;
    passed: boolean;
    ran_at: string;
  }>;
};

export type ExceptionReport = {
  run_id: string;
  total_cases: number;
  total_amount_at_risk_paise: number;
  groups: Array<{
    reason: string;
    detail: string;
    cases: number;
    amount_at_risk_paise: number;
  }>;
  items: Array<{
    case_id: string;
    reason: string;
    detail: string;
    leak_type: string;
    state: string;
    outcome: string | null;
    amount_at_risk_paise: number;
  }>;
};
