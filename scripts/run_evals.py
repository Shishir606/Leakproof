"""Run the September 1 cohort and prompt-injection acceptance suites."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy.orm import Session

from leakproof.db import build_engine
from leakproof.evals import run_all_evals


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", type=Path, default=Path("evals/cohort/cases.jsonl"))
    parser.add_argument("--injection", type=Path, default=Path("evals/injection/corpus.jsonl"))
    parser.add_argument("--decision", type=Path, default=Path("evals/decision_quality/cases.jsonl"))
    parser.add_argument("--baseline", type=Path, default=Path("evals/baseline.json"))
    parser.add_argument("--report", type=Path, default=Path("evals/report.json"))
    parser.add_argument("--no-persist", action="store_true")
    arguments = parser.parse_args()

    if arguments.no_persist:
        report = run_all_evals(
            cohort_path=arguments.cohort,
            injection_path=arguments.injection,
            decision_path=arguments.decision,
            baseline_path=arguments.baseline,
            report_path=arguments.report,
        )
    else:
        engine = build_engine()
        try:
            with Session(engine, expire_on_commit=False) as session:
                report = run_all_evals(
                    cohort_path=arguments.cohort,
                    injection_path=arguments.injection,
                    decision_path=arguments.decision,
                    baseline_path=arguments.baseline,
                    report_path=arguments.report,
                    session=session,
                )
        finally:
            engine.dispose()

    print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
    if not report.overall_passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
