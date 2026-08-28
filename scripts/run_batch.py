"""Run the reproducible September 4 synthetic batch and print defensible metrics."""

from __future__ import annotations

import argparse
import json

from sqlalchemy.orm import Session

from leakproof.batch import run_full_batch
from leakproof.db import build_engine
from leakproof.measurement import compute_scoreboard, exception_report
from leakproof.simulator.generate import generate_dataset, load_parameters


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    arguments = parser.parse_args()
    parameters = load_parameters()
    parameters = parameters.model_copy(
        update={
            "simulation": parameters.simulation.model_copy(update={"seed": arguments.seed})
        }
    )
    dataset = generate_dataset(parameters)
    engine = build_engine()
    try:
        with Session(engine, expire_on_commit=False) as session:
            result = run_full_batch(session, dataset, parameters)
            exceptions = exception_report(session, dataset.run_id)
            payload = {
                "run": result.as_dict(),
                "scoreboard": compute_scoreboard(session, dataset.run_id).model_dump(mode="json"),
                "exceptions": exceptions.model_dump(mode="json", exclude={"items"}),
                "exception_case_endpoint": (
                    f"/scoreboard/{dataset.run_id}/exceptions"
                ),
            }
    finally:
        engine.dispose()
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
