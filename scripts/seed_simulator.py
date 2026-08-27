"""Create and optionally persist the reproducible August 26 synthetic merchant."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy.orm import Session

from leakproof.db import build_engine
from leakproof.simulator.generate import generate_dataset, load_parameters
from leakproof.simulator.seed import persist_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, help="Override the fixed seed in simulator/params.yaml")
    parser.add_argument(
        "--params",
        type=Path,
        default=Path("simulator/params.yaml"),
        help="Path to the human-readable simulator assumptions",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Synthetic artifact path (default: artifacts/simulator/seed-<seed>.json)",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Generate the deterministic artifact without connecting to PostgreSQL",
    )
    arguments = parser.parse_args()

    parameters = load_parameters(arguments.params)
    dataset = generate_dataset(parameters, seed=arguments.seed)
    artifact_path = arguments.output or Path(f"artifacts/simulator/seed-{dataset.seed}.json")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(dataset.artifact(), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    summary = dataset.summary()
    summary["artifact"] = str(artifact_path)
    if not arguments.no_persist:
        engine = build_engine()
        try:
            with Session(engine, expire_on_commit=False) as session:
                summary["persistence"] = persist_dataset(session, dataset).as_dict()
        finally:
            engine.dispose()
    else:
        summary["persistence"] = "skipped"

    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
