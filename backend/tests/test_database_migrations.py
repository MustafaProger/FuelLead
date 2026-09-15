import pytest
from pathlib import Path
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app import database
from app.models import ALL_STATUSES, REMOVED_COMPANY_STATUSES, Company


def test_company_status_migration_preserves_rows_and_child_foreign_keys(tmp_path, monkeypatch):
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    timestamps = "2026-09-01 09:00:00+00:00"
    with legacy_engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE companies (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(400) NOT NULL,
                    inn VARCHAR(12) NOT NULL UNIQUE,
                    ogrn VARCHAR(15),
                    primary_okved_code VARCHAR(20),
                    primary_okved_name VARCHAR(500),
                    activity_category VARCHAR(40),
                    is_active BOOLEAN NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    provider VARCHAR(30) NOT NULL,
                    first_discovered_at DATETIME NOT NULL,
                    last_checked_at DATETIME NOT NULL,
                    last_updated_at DATETIME NOT NULL,
                    CONSTRAINT ck_companies_status CHECK (
                        status IN ('new','checked','ready','sent','answered','interested','rejected','error')
                    )
                )
                """
            )
        )
        connection.execute(text("CREATE INDEX ix_companies_status ON companies (status)"))
        connection.execute(
            text(
                """
                CREATE TABLE company_emails (
                    id INTEGER PRIMARY KEY,
                    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                    email VARCHAR(320) NOT NULL
                )
                """
            )
        )
        for company_id, status in ((1, "ready"), (2, "sent"), (3, "error")):
            connection.execute(
                text(
                    """
                    INSERT INTO companies (
                        id, name, inn, activity_category, is_active, status, provider,
                        first_discovered_at, last_checked_at, last_updated_at
                    ) VALUES (
                        :id, :name, :inn, 'freight', 1, :status, 'checko',
                        :timestamp, :timestamp, :timestamp
                    )
                    """
                ),
                {
                    "id": company_id,
                    "name": f"Компания {company_id}",
                    "inn": f"770000000{company_id}",
                    "status": status,
                    "timestamp": timestamps,
                },
            )
        connection.execute(
            text("INSERT INTO company_emails (id, company_id, email) VALUES (1, 1, 'lead@example.ru')")
        )

    monkeypatch.setattr(database, "engine", legacy_engine)
    database._upgrade_company_status_schema(
        Company,
        ALL_STATUSES,
        REMOVED_COMPANY_STATUSES,
    )

    with legacy_engine.connect() as connection:
        statuses = connection.execute(
            text("SELECT id, status FROM companies ORDER BY id")
        ).all()
        child = connection.execute(
            text("SELECT company_id, email FROM company_emails")
        ).one()
        foreign_key_target = connection.execute(
            text("PRAGMA foreign_key_list(company_emails)")
        ).mappings().one()["table"]
        status_check = next(
            item
            for item in inspect(connection).get_check_constraints("companies")
            if item["name"] == "ck_companies_status"
        )

    assert statuses == [(1, "new"), (2, "sent"), (3, "new")]
    assert child == (1, "lead@example.ru")
    assert foreign_key_target == "companies"
    assert "customer" in status_check["sqltext"]
    assert "error" not in status_check["sqltext"]
    assert "ready" not in status_check["sqltext"]

    with legacy_engine.begin() as connection:
        connection.execute(text("UPDATE companies SET status = 'customer' WHERE id = 1"))

    with pytest.raises(IntegrityError):
        with legacy_engine.begin() as connection:
            connection.execute(text("UPDATE companies SET status = 'ready' WHERE id = 1"))


def test_postgresql_mail_scheduler_migration_preserves_legacy_rows():
    sql = (
        Path(__file__).parents[1]
        / "app"
        / "migrations"
        / "20260903_mailru_scheduler.sql"
    ).read_text(encoding="utf-8")

    assert "UPDATE outreach_deliveries SET status = 'accepted'" in sql
    assert "accepted_at = COALESCE(accepted_at, sent_at)" in sql
    assert "UPDATE outreach_campaigns SET accepted_count = sent_count" in sql
    assert "UPDATE outreach_campaigns SET status = 'stopped' WHERE status = 'cancelled'" in sql
    assert "DROP CONSTRAINT IF EXISTS ck_outreach_deliveries_status" in sql
    assert "DROP CONSTRAINT IF EXISTS ck_outreach_campaigns_status" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_outreach_campaign" in sql
    assert "DROP TABLE" not in sql.upper()


def test_imap_namespace_upgrade_is_additive_and_idempotent(tmp_path, monkeypatch):
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy-mail.db'}")
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE sender_accounts (id INTEGER PRIMARY KEY, email TEXT NOT NULL, imap_last_uid BIGINT NOT NULL)")
        connection.exec_driver_sql("INSERT INTO sender_accounts VALUES (1, 'sender@mail.ru', 123)")
        connection.exec_driver_sql("CREATE TABLE imap_processed_messages (id INTEGER PRIMARY KEY, sender_account_id INTEGER NOT NULL REFERENCES sender_accounts(id), uid BIGINT NOT NULL)")
        connection.exec_driver_sql("INSERT INTO imap_processed_messages VALUES (1, 1, 123)")
    monkeypatch.setattr(database, "engine", legacy_engine)

    database._upgrade_imap_uidvalidity_schema()
    database._upgrade_imap_uidvalidity_schema()

    with legacy_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT id, email, imap_last_uid, imap_uidvalidity FROM sender_accounts").one() == (1, "sender@mail.ru", 123, None)
        assert connection.exec_driver_sql("SELECT sender_account_id, uid FROM imap_processed_messages").one() == (1, 123)


def test_postgresql_imap_namespace_migration_preserves_existing_cursors():
    sql = (Path(__file__).parents[1] / "app" / "migrations" / "20260911_imap_uidvalidity.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS imap_uidvalidity BIGINT" in sql
    assert "20260911_imap_uidvalidity" in sql
    assert "DELETE " not in sql.upper()
    assert "UPDATE " not in sql.upper()
    assert "DROP " not in sql.upper()


def test_sqlite_provider_upgrade_preserves_mailbox_and_delivery_links(tmp_path, monkeypatch):
    from app.models import SenderAccount
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.orm import Session
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'providers.db'}")
    ddl = str(CreateTable(SenderAccount.__table__).compile(legacy_engine)).replace(
        "'gmail_api','mailru_smtp','gmail_smtp','yandex_smtp'", "'gmail_api','mailru_smtp'"
    )
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql(ddl)
        connection.exec_driver_sql('CREATE UNIQUE INDEX ix_sender_accounts_email ON sender_accounts(email)')
        connection.exec_driver_sql('CREATE TABLE delivery_links (id INTEGER PRIMARY KEY, sender_account_id INTEGER REFERENCES sender_accounts(id))')
    with Session(legacy_engine) as session:
        session.add(SenderAccount(email='owner@mail.ru', encrypted_password='fake-cipher', sent_today=23, imap_last_uid=400, imap_uidvalidity=19))
        session.commit()
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql('INSERT INTO delivery_links VALUES (1, 1)')
        before = connection.exec_driver_sql('SELECT * FROM sender_accounts').all()
    monkeypatch.setattr(database, 'engine', legacy_engine)
    database._upgrade_sqlite_sender_providers()
    database._upgrade_sqlite_sender_providers()
    with legacy_engine.connect() as connection:
        assert connection.exec_driver_sql('SELECT * FROM sender_accounts').all() == before
        assert connection.exec_driver_sql('SELECT * FROM delivery_links').all() == [(1, 1)]
        assert connection.exec_driver_sql('PRAGMA foreign_key_check').all() == []
        assert inspect(connection).get_foreign_keys('delivery_links')[0]['referred_table'] == 'sender_accounts'
        assert inspect(connection).get_indexes('sender_accounts')[0]['unique']
    with Session(legacy_engine) as session:
        session.add_all([SenderAccount(provider='gmail_smtp', email='new@gmail.com'), SenderAccount(provider='yandex_smtp', email='new@ya.ru')])
        session.commit()
    with pytest.raises(IntegrityError), Session(legacy_engine) as session:
        session.add(SenderAccount(provider='unknown', email='no@example.org'))
        session.commit()
