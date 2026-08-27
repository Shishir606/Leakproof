"""Deterministic, auditable case diagnosis."""

from leakproof.diagnosis.tier1 import (
    DiagnosisResult,
    classify_payment_failure,
    classify_receivable,
    diagnose_case,
)
from leakproof.diagnosis.tier2 import (
    CohortAnomaly,
    CohortRunResult,
    CohortScanInput,
    CohortScanOutput,
    DeterministicCohortTransport,
    StructuredLLMClient,
    aggregate_cohort_window,
    case_matches_open_suppression,
    qualified_slices,
    run_cohort_scan,
)

__all__ = [
    "DiagnosisResult",
    "classify_payment_failure",
    "classify_receivable",
    "diagnose_case",
    "CohortAnomaly",
    "CohortRunResult",
    "CohortScanInput",
    "CohortScanOutput",
    "DeterministicCohortTransport",
    "StructuredLLMClient",
    "aggregate_cohort_window",
    "case_matches_open_suppression",
    "qualified_slices",
    "run_cohort_scan",
]
