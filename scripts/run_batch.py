"""Run the reproducible September 4 synthetic batch and print defensible metrics."""

from __future__ import annotations

import argparse
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leakproof.batch import run_full_batch
from leakproof.db import build_engine
from leakproof.measurement import compute_scoreboard, exception_report
from leakproof.models.db import Action, Event, RecoveryAttribution
from leakproof.simulator.generate import generate_dataset, load_parameters


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--verify-replay",
        action="store_true",
        help="Run the same batch twice and fail if the second pass changes durable output.",
    )
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
            scoreboard = compute_scoreboard(session, dataset.run_id)
            payload = {
                "run": result.as_dict(),
                "scoreboard": scoreboard.model_dump(mode="json"),
                "exceptions": exceptions.model_dump(mode="json", exclude={"items"}),
                "exception_case_endpoint": (
                    f"/scoreboard/{dataset.run_id}/exceptions"
                ),
            }
            if arguments.verify_replay:
                counts_before = {
                    "events": int(session.scalar(select(func.count()).select_from(Event)) or 0),
                    "actions": int(session.scalar(select(func.count()).select_from(Action)) or 0),
                    "attributions": int(
                        session.scalar(select(func.count()).select_from(RecoveryAttribution)) or 0
                    ),
                }
                replay = run_full_batch(session, dataset, parameters)
                counts_after = {
                    "events": int(session.scalar(select(func.count()).select_from(Event)) or 0),
                    "actions": int(session.scalar(select(func.count()).select_from(Action)) or 0),
                    "attributions": int(
                        session.scalar(select(func.count()).select_from(RecoveryAttribution)) or 0
                    ),
                }
                replay_scoreboard = compute_scoreboard(session, dataset.run_id)
                if not replay.replayed:
                    raise RuntimeError("second batch execution did not report replay mode")
                if counts_after != counts_before:
                    raise RuntimeError(
                        f"batch replay changed durable counts: {counts_before} -> {counts_after}"
                    )
                if replay_scoreboard != scoreboard:
                    raise RuntimeError("batch replay changed the published scoreboard")
                payload["replay_verification"] = {
                    "passed": True,
                    "second_run_replayed": True,
                    "durable_counts_unchanged": counts_after,
                    "scoreboard_unchanged": True,
                }
    finally:
        engine.dispose()
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
