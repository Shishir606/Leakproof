"""Run isolated multi-seed synthetic economics with three treatment-effect assumptions."""

# ruff: noqa: E402 -- simulation mode must be fixed before application imports initialize settings.

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

# This command is intentionally synthetic even when the developer's .env points at live_demo.
# The environment override is established before any cached application settings are loaded.
os.environ["LEAKPROOF_MODE"] = "simulation"

from leakproof.batch import run_full_batch
from leakproof.db import Base, build_engine
from leakproof.measurement import compute_scoreboard
from leakproof.measurement.sensitivity import SensitivityReport, summarize_scenario
from leakproof.simulator.config import SimulatorParameters, TreatmentEffectConfig
from leakproof.simulator.generate import generate_dataset, load_parameters


def _scaled_parameters(
    parameters: SimulatorParameters,
    *,
    seed: int,
    multiplier: float,
) -> SimulatorParameters:
    effects = parameters.treatment_effect
    scaled = {
        action: {
            failure_class: min(1.0, max(0.0, rate * multiplier))
            for failure_class, rate in getattr(effects, action).items()
        }
        for action in ("silent_retry", "whatsapp_link", "sms_link", "voice_hinglish")
    }
    treatment_effect = TreatmentEffectConfig(
        **scaled,
        fatigue_penalty_per_extra_contact=effects.fatigue_penalty_per_extra_contact,
        opt_out_prob_per_contact=effects.opt_out_prob_per_contact,
    )
    return parameters.model_copy(
        update={
            "simulation": parameters.simulation.model_copy(update={"seed": seed}),
            "treatment_effect": treatment_effect,
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", default="41,42,43,44,45")
    parser.add_argument("--multipliers", default="0.25,1.0,1.25")
    parser.add_argument("--report", type=Path, default=Path("samples/day3-sensitivity.json"))
    args = parser.parse_args()
    seeds = [int(item) for item in args.seeds.split(",") if item.strip()]
    multipliers = [float(item) for item in args.multipliers.split(",") if item.strip()]
    if len(seeds) < 2:
        parser.error("at least two seeds are required")
    if len(multipliers) != 3:
        parser.error("exactly three treatment-effect multipliers are required")

    base = load_parameters()
    scenarios = []
    # A temporary database is the isolation boundary: no live or developer tables are touched.
    with tempfile.TemporaryDirectory(prefix="leakproof-sensitivity-") as directory:
        engine = build_engine(f"sqlite+pysqlite:///{Path(directory) / 'evaluation.db'}")
        Base.metadata.create_all(engine)
        try:
            for multiplier in multipliers:
                scoreboards = []
                for seed in seeds:
                    parameters = _scaled_parameters(base, seed=seed, multiplier=multiplier)
                    dataset = generate_dataset(parameters)
                    with Session(engine, expire_on_commit=False) as session:
                        run_full_batch(session, dataset, parameters)
                        scoreboards.append(compute_scoreboard(session, dataset.run_id))
                scenarios.append(
                    summarize_scenario(
                        scoreboards,
                        seeds=seeds,
                        treatment_effect_multiplier=multiplier,
                    )
                )
        finally:
            engine.dispose()

    report = SensitivityReport(scenarios=scenarios)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
