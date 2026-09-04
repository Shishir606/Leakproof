"""Run the existing automated gate in a credential-free copy and a separate Compose stack."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="leakproof-release-"))
    project = work.name
    snapshot = work / "project"
    evidence = ROOT / "artifacts" / "baseline" / project
    evidence.mkdir(parents=True)
    shutil.copytree(
        ROOT,
        snapshot,
        ignore=shutil.ignore_patterns(
            ".git", ".env", ".env.*", ".venv", "node_modules", ".next", "artifacts",
            "__pycache__", ".pytest_cache", ".ruff_cache", ".coverage", "*.tsbuildinfo",
            ".DS_Store",
        ),
    )
    db_port, api_port, dashboard_port = free_port(), free_port(), free_port()
    safe = {
        "LEAKPROOF_MODE": "simulation",
        "LEAKPROOF_ENVIRONMENT": "test",
        "LEAKPROOF_OPERATOR_API_TOKEN": "isolated-release-test-operator-credential",
        "LEAKPROOF_OPERATOR_MERCHANT_IDS": "merchant_demo",
        # Match the existing webhook test fixture, also used by the foundation verifier.
        "LEAKPROOF_RAZORPAY_WEBHOOK_SECRET": "test-secret",
        "LEAKPROOF_RECOVERY_TOKEN_SECRET": "isolated-release-test-recovery-secret",
        "LEAKPROOF_RAZORPAY_KEY_ID": "",
        "LEAKPROOF_RAZORPAY_KEY_SECRET": "",
        "LEAKPROOF_OPENAI_API_KEY": "",
        "LEAKPROOF_RESEND_API_KEY": "",
        "LEAKPROOF_RESEND_WEBHOOK_SECRET": "",
        "LEAKPROOF_RESEND_FROM_EMAIL": "",
        "LEAKPROOF_DEMO_EMAIL_ALLOWLIST": "",
    }
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    compose["name"] = project
    compose.pop("x-app")
    for name, service in compose["services"].items():
        if name in {"api", "worker", "beat", "migrate"}:
            service["image"] = f"{project}-app:latest"
            service["env_file"] = []
            service["environment"] = {
                **service["environment"], **safe,
                "LEAKPROOF_LUNA_ENABLED": "false",
                "LEAKPROOF_OUTBOUND_EMAIL_ENABLED": "false",
            }
        service["restart"] = "no"
    for name, host_port, container_port in (
        ("postgres", db_port, 5432), ("api", api_port, 8000),
        ("dashboard", dashboard_port, 3000),
    ):
        compose["services"][name]["ports"] = [f"127.0.0.1:{host_port}:{container_port}"]
    compose["services"]["dashboard"]["environment"] = {
        "API_BASE_URL": "http://api:8000",
        "HOSTNAME": "0.0.0.0",
        "LEAKPROOF_OPERATOR_API_TOKEN": safe["LEAKPROOF_OPERATOR_API_TOKEN"],
        "LEAKPROOF_OPERATOR_UI_ENABLED": "false",
    }
    (snapshot / "docker-compose.yml").write_text(yaml.safe_dump(compose, sort_keys=False))
    # Change only infrastructure addresses and output locations in the copied gate.
    makefile = (ROOT / "Makefile").read_text().replace("localhost:55432", f"localhost:{db_port}")
    makefile = makefile.replace(
        "scripts/verify_fresh_migrations.py",
        "scripts/verify_fresh_migrations.py --admin-url "
        f"postgresql+psycopg://leakproof:leakproof@localhost:{db_port}/postgres",
    )
    for name in ("batch", "evals"):
        makefile = makefile.replace(
            f"/tmp/leakproof-release-gate-{name}.json", str(work / f"{name}.json")
        )
    (snapshot / "Makefile").write_text(makefile)
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("LEAKPROOF_", "COMPOSE_", "MAKE", "NEXT_PUBLIC_"))
        and key not in {"API_BASE_URL", "UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV"}
    }
    environment.update(safe)
    environment.update({
        "API_BASE_URL": f"http://localhost:{api_port}",
        "LEAKPROOF_DATABASE_URL":
            f"postgresql+psycopg://leakproof:leakproof@localhost:{db_port}/leakproof",
        "LEAKPROOF_VERIFY_API_URL": f"http://localhost:{api_port}",
        "LEAKPROOF_VERIFY_DATABASE_URL":
            f"postgresql://leakproof:leakproof@localhost:{db_port}/leakproof",
        "NEXT_TELEMETRY_DISABLED": "1",
    })
    summary = {"started_at": datetime.now(UTC).isoformat(), "project": project,
               "snapshot": str(snapshot), "provider_calls": "disabled; simulation only"}
    print(f"Isolated release evidence: {evidence}", flush=True)
    result = 1
    cleanup = 1
    try:
        with (evidence / "release-gate.log").open("w") as log:
            for command in (
                ["uv", "sync", "--extra", "dev", "--frozen"],
                ["npm", "--prefix", "dashboard", "ci", "--no-audit", "--no-fund"],
                ["make", "release-gate-automated"],
            ):
                result = subprocess.run(
                    command, cwd=snapshot, env=environment, stdout=log,
                    stderr=subprocess.STDOUT, check=False,
                ).returncode
                if result:
                    break
    finally:
        # Stop only this run's containers. Retain the volume and snapshot for audit/debugging.
        with (evidence / "cleanup.log").open("w") as log:
            cleanup = subprocess.run(
                ["docker", "compose", "down"], cwd=snapshot, env=environment,
                stdout=log, stderr=subprocess.STDOUT, check=False,
            ).returncode
        for source in (
            work / "batch.json", work / "evals.json",
            snapshot / "artifacts" / "ai-acceptance" / "cohort-incident.json",
        ):
            if source.exists():
                shutil.copy2(source, evidence / source.name)
        contract_evidence = snapshot / "artifacts" / "release-contract"
        if contract_evidence.exists():
            shutil.copytree(contract_evidence, evidence / "release-contract")
        summary.update({"finished_at": datetime.now(UTC).isoformat(), "exit_code": result,
                        "cleanup_exit_code": cleanup, "passed": result == 0 and cleanup == 0})
        (evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"Automated gate {'passed' if summary['passed'] else 'failed'}; see {evidence}")
    return result or cleanup


if __name__ == "__main__":
    raise SystemExit(main())
