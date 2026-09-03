"""Disposable PostgreSQL gates; never migrate the developer's application database.

Set LEAKPROOF_TEST_POSTGRES_ADMIN_URL to run. The existing migration verifier runs
this module after its fresh-install passes.
"""

from __future__ import annotations

import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from test_multi_resource_foundation import payment, risk

from leakproof.models.db import ProviderObligation, RecoveryCase, Settlement
from leakproof.provider_resources import record_recovery, record_risk


@pytest.fixture(params=["fresh", "upgrade"])
def migrated_postgres(request, monkeypatch):
    admin_url = os.environ.get("LEAKPROOF_TEST_POSTGRES_ADMIN_URL")
    if not admin_url:
        pytest.skip("requires disposable PostgreSQL admin URL")
    database_name = f"leakproof_foundation_{secrets.token_hex(6)}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    database_url = make_url(admin_url).set(database=database_name)
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
    engine = create_engine(database_url)
    config = Config("alembic.ini")
    monkeypatch.setenv("LEAKPROOF_DATABASE_URL", database_url.render_as_string(hide_password=False))
    from leakproof.config import get_settings

    get_settings.cache_clear()
    before = None
    try:
        if request.param == "upgrade":
            with engine.begin() as connection:
                # Frozen actual 0010 metadata, not 0001's dynamic import of today's models.
                for statement in Path("tests/fixtures/schema_0010.sql").read_text().split(";"):
                    if statement.strip():
                        connection.exec_driver_sql(statement)
                connection.exec_driver_sql("""
                    INSERT INTO merchants (id,name,policy,created_at)
                    VALUES ('merchant_upgrade','Upgrade','{}',CURRENT_TIMESTAMP)
                """)
                connection.exec_driver_sql("""
                    INSERT INTO customers (id,merchant_id,locale,protected,dnc,created_at)
                    VALUES ('customer_upgrade','merchant_upgrade','en-IN',false,false,
                    CURRENT_TIMESTAMP)
                """)
                for state in ("CREATED", "CHECKOUT_OPEN", "AT_RISK", "RECOVERED", "EXPIRED"):
                    connection.execute(
                        text("""
                        INSERT INTO demo_sessions (id,merchant_id,customer_id,razorpay_order_id,
                        amount_paise,currency,state,recipient_ciphertext,recipient_hash,
                        expires_at,created_at,updated_at)
                        VALUES (:id,'merchant_upgrade','customer_upgrade',:oid,50000,'INR',:state,
                        'encrypted-historical-recipient','historical-hash',
                        CURRENT_TIMESTAMP + INTERVAL '1 hour',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
                    """),
                        {"id": f"demo_{state}", "oid": f"order_{state}", "state": state},
                    )
                connection.exec_driver_sql("""
                    INSERT INTO cases (id,merchant_id,customer_id,leak_type,entity_type,entity_id,
                    dedupe_key,amount_band,amount_at_risk,currency,state,arm,outcome,
                    detected_at,closed_at,attribution_until)
                    VALUES ('case_old','merchant_upgrade','customer_upgrade','PAYMENT_FAILURE',
                    'payment','pay_old','live:demo_RECOVERED:order_RECOVERED','LOW',50000,'INR',
                    'CLOSED','TREATMENT','RECOVERED',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP + INTERVAL '1 day')
                """)
                connection.exec_driver_sql("""
                    INSERT INTO events (case_id,seq,kind,payload,actor,occurred_at)
                    VALUES ('case_old',1,'CLOSED','{"outcome":"RECOVERED"}',
                    'verifier',CURRENT_TIMESTAMP)
                """)
                connection.exec_driver_sql("""
                    INSERT INTO actions (id,case_id,step_index,action_type,scheduled_for,
                    idempotency_key,status,attempt_count,cost_paise)
                    VALUES ('action_old','case_old',0,
                    'email_link',CURRENT_TIMESTAMP,'old-idempotency','cancelled',0,0)
                """)
                connection.exec_driver_sql("""
                    INSERT INTO recovery_attributions (case_id,payment_entity_id,amount_paise,
                    matched_by,credit_rule,organic,paid_at,attributed_at) VALUES
                    ('case_old','pay_old',50000,'entity_id','last_touch',true,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
                """)
                connection.exec_driver_sql("""
                    INSERT INTO email_deliveries (session_id,case_id,action_id,recipient_hash,
                    status,created_at,updated_at) VALUES ('demo_RECOVERED','case_old','action_old',
                    'historical-hash','preview_only',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
                """)
                append_only = (
                    ScriptDirectory.from_config(config)
                    .get_revision("0002_append_only_events")
                    .module
                )
                with Operations.context(MigrationContext.configure(connection)):
                    append_only.upgrade()
                before = snapshot_history(connection)
            command.stamp(config, "0010_payment_attempts")
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        yield engine, before
    finally:
        engine.dispose()
        get_settings.cache_clear()
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=:name AND pid<>pg_backend_pid()"
                ),
                {"name": database_name},
            )
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        admin.dispose()


def snapshot_history(connection):
    return {
        table: connection.exec_driver_sql(f"SELECT * FROM {table}").mappings().all()
        for table in ("cases", "events", "actions", "recovery_attributions", "email_deliveries")
    }


def test_fresh_and_frozen_upgrade_preserve_history(migrated_postgres):
    engine, before = migrated_postgres
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0011_multi_resource"
        )
        assert {"provider_entities", "provider_obligations", "provider_settlements"} <= set(
            inspect(connection).get_table_names()
        )
        assert next(
            col
            for col in inspect(connection).get_columns("demo_sessions")
            if col["name"] == "razorpay_order_id"
        )["nullable"]
        if before:
            assert snapshot_history(connection) == before
            with pytest.raises(DBAPIError, match="append-only"), connection.begin_nested():
                connection.exec_driver_sql(
                    "UPDATE events SET actor='changed' WHERE case_id='case_old'"
                )
            rows = (
                connection.execute(text("SELECT * FROM demo_sessions ORDER BY id")).mappings().all()
            )
            assert len(rows) == 5
            assert all(row["primary_entity_id"] == row["razorpay_order_id"] for row in rows)
            assert all(row["scenario_type"] == "PAYMENT_FAILURE" for row in rows)
            assert all(row["setup_state"] == "READY" for row in rows)
            assert all(row["capability_evidence"] == "ARCHITECTURE_READY" for row in rows)
            assert connection.scalar(text("SELECT count(*) FROM provider_entities")) == 5
            obligation = (
                connection.execute(
                    text("SELECT * FROM provider_obligations WHERE case_id='case_old'")
                )
                .mappings()
                .one()
            )
            assert obligation["recovered_paise"] == 50000 and obligation["settled_at"]


def test_concurrent_cases_and_settlements_have_one_owner(migrated_postgres):
    engine, _ = migrated_postgres
    factory = sessionmaker(engine, expire_on_commit=False)

    def detect(_):
        with factory.begin() as session:
            return record_risk(session, risk())[0].id

    with ThreadPoolExecutor(max_workers=4) as pool:
        owners = list(pool.map(detect, range(4)))
    assert len(set(owners)) == 1

    def settle(_):
        with factory.begin() as session:
            return record_recovery(session, payment(amount=80, due=0)).id

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert set(pool.map(settle, range(4))) == set(owners)
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(Settlement)
                .where(Settlement.merchant_id == "merchant_resources")
            )
            == 1
        )
        assert (
            session.scalar(
                select(Settlement.credited_paise).where(
                    Settlement.merchant_id == "merchant_resources"
                )
            )
            == 80
        )
        assert (
            session.scalar(
                select(ProviderObligation.case_id).where(
                    ProviderObligation.id.like("obl_%"),
                    ProviderObligation.provider_entity_id == "inv_cycle_1",
                )
            )
            == owners[0]
        )
        assert session.get(RecoveryCase, owners[0]).outcome == "RECOVERED"


def test_preloaded_obligation_is_refreshed_under_concurrent_credit_lock(migrated_postgres):
    from threading import Barrier

    from test_multi_resource_foundation import INVOICE, SCOPE

    engine, _ = migrated_postgres
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        case, _ = record_risk(session, risk())
        owner = case.id
    barrier = Barrier(3)

    def settle(index):
        with factory.begin() as session:
            stale = session.get(ProviderObligation, SCOPE.identity(INVOICE))
            assert stale.recovered_paise == 0
            barrier.wait(timeout=10)
            record_recovery(
                session,
                payment(payment_id=f"pay_concurrent_{index}", amount=[30, 50, 100][index], due=0),
            )

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(settle, range(3)))
    with factory() as session:
        assert (
            session.scalar(
                select(func.sum(Settlement.credited_paise)).where(
                    Settlement.merchant_id == SCOPE.merchant_id
                )
            )
            == 80
        )
        assert session.get(ProviderObligation, SCOPE.identity(INVOICE)).case_id == owner


def test_track_a_concurrent_dismissal_and_scheduled_redispatch(migrated_postgres):
    from datetime import timedelta

    from test_api_august_30 import NOW, checkout_event, create_session

    from leakproof.demo.service import (
        due_abandonment_checks,
        ingest_checkout_event,
        materialize_checkout_abandonment,
    )
    from leakproof.models.db import ProviderCall

    engine, _ = migrated_postgres
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        created, provider, limiter, settings = create_session(session)
        dismissed = ingest_checkout_event(
            session,
            created.session_id,
            checkout_event("checkout_dismissed", "pg-dismiss"),
            session_token=created.session_token,
            limiter=limiter,
            settings=settings,
            now=NOW,
        )
        due = NOW + timedelta(seconds=31)
        assert due_abandonment_checks(session, settings=settings, now=due) == [
            (created.session_id, dismissed.dismissal_event_id)
        ]

    def deliver(_):
        with factory() as session:
            return materialize_checkout_abandonment(
                session,
                created.session_id,
                dismissed.dismissal_event_id,
                provider=provider,
                settings=settings,
                now=due,
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        owners = list(pool.map(deliver, range(4)))
    assert owners[0] is not None and len(set(owners)) == 1
    with factory() as session:
        assert not due_abandonment_checks(session, settings=settings, now=due)
        assert (
            session.scalar(
                select(func.count())
                .select_from(ProviderCall)
                .where(
                    ProviderCall.session_id == created.session_id,
                    ProviderCall.operation == "list_order_payments",
                )
            )
            == 1
        )


def test_track_b_concurrent_reconciliation_and_email_share_one_invoice(migrated_postgres):
    from datetime import timedelta

    from test_api_september_4 import NOW
    from test_track_b_invoices import pay, reconcile, setup_invoice

    from leakproof.demo.email import execute_demo_recovery_email, schedule_demo_recovery_email
    from leakproof.diagnosis import diagnose_case
    from leakproof.providers.fakes import FakeEmailProvider, FakePaymentProvider

    engine, _ = migrated_postgres
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    provider, email = FakePaymentProvider(), FakeEmailProvider()
    with factory() as session:
        _, demo, config = setup_invoice(session, provider, recipient="reviewer@example.com")
        case = reconcile(session, demo, provider, config)
        diagnose_case(session, case.id)
        action = schedule_demo_recovery_email(
            session, case.id, settings=config, now=NOW + timedelta(seconds=61)
        )
        demo_id, case_id, action_id = demo.id, case.id, action.id
        pay(provider, demo, 20000)

    def check():
        from leakproof.models.db import DemoSession

        with factory() as session:
            demo = session.get(DemoSession, demo_id)
            return reconcile(session, demo, provider, config, 120).id

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert set(pool.map(lambda _: check(), range(4))) == {case_id}
    with factory() as session:
        obligation = session.scalar(
            select(ProviderObligation).where(ProviderObligation.case_id == case_id)
        )
        assert obligation.outstanding_paise == 30000
        assert obligation.recovered_paise == 20000
        assert obligation.detected_due_paise == 50000
        assert (
            session.scalar(
                select(func.count(Settlement.id)).where(Settlement.obligation_id == obligation.id)
            )
            == 1
        )
        pay(provider, demo, 50000, payment_id="pay_final", amount=30000, seconds=100)

    def contact():
        with factory() as session:
            return execute_demo_recovery_email(
                session,
                action_id,
                provider=email,
                invoice_provider=provider,
                settings=config,
                now=NOW + timedelta(seconds=120),
            ).status

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(check), pool.submit(contact), pool.submit(check)]
        assert [f.result(timeout=10) for f in futures] == [case_id, "cancelled", case_id]
    with factory() as session:
        obligation = session.scalar(
            select(ProviderObligation).where(ProviderObligation.case_id == case_id)
        )
        assert obligation.recovered_paise == 50000 and obligation.outstanding_paise == 0
        assert session.get(RecoveryCase, case_id).outcome == "RECOVERED"
        assert (
            session.scalar(
                select(func.count(Settlement.id)).where(Settlement.obligation_id == obligation.id)
            )
            == 2
        )
        assert not email.calls
