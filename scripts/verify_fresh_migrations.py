"""Verify fresh and frozen-0010 upgrades on disposable PostgreSQL databases."""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--admin-url",
        default="postgresql+psycopg://leakproof:leakproof@localhost:55432/postgres",
        help="PostgreSQL administrator URL used only to create a disposable database.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database_name = f"leakproof_release_{os.getpid()}_{secrets.token_hex(4)}"
    admin = create_engine(args.admin_url, isolation_level="AUTOCOMMIT")
    release_url = make_url(args.admin_url).set(database=database_name)
    environment = os.environ.copy()
    environment.update(
        {
            "LEAKPROOF_DATABASE_URL": release_url.render_as_string(hide_password=False),
            "LEAKPROOF_MODE": "simulation",
        }
    )
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
        for pass_number in (1, 2):
            completed = subprocess.run(
                ["uv", "run", "alembic", "upgrade", "head"],
                env=environment,
                check=False,
            )
            if completed.returncode:
                print(f"migration pass {pass_number} failed", file=sys.stderr)
                return completed.returncode
        release = create_engine(release_url)
        try:
            with release.connect() as connection:
                revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
                table_count = connection.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
        finally:
            release.dispose()
        print(
            f"fresh and reused migration passes succeeded at {revision} "
            f"with {table_count} public tables"
        )
        upgrade_environment = environment.copy()
        upgrade_environment["LEAKPROOF_TEST_POSTGRES_ADMIN_URL"] = args.admin_url
        return subprocess.run(
            ["uv", "run", "pytest", "tests/test_multi_resource_migrations.py", "-q"],
            env=upgrade_environment,
            check=False,
        ).returncode
    finally:
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        admin.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
