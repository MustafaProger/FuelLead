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

    assert statuses == [(1, "new"), (2, "sent"), (3, "error")]
    assert child == (1, "lead@example.ru")
    assert foreign_key_target == "companies"
    assert "customer" in status_check["sqltext"]
    assert "error" in status_check["sqltext"]
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
