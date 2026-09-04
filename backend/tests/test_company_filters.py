import pytest

from app.config import Settings
from app.main import update_company_status
from app.models import Company, CompanyEmail
from app.queries import build_company_query
from app.schemas import CompanyFilters, StatusUpdate


def add_company(db, name: str, inn: str, *emails: str) -> Company:
    company = Company(name=name, inn=inn)
    company.emails.extend(CompanyEmail(email=email) for email in emails)
    db.add(company)
    db.commit()
    return company


def company_names(db, provider: str) -> set[str]:
    query = build_company_query(CompanyFilters(email_provider=provider))
    return {company.name for company in db.scalars(query).all()}


def test_email_provider_filter_covers_public_and_other_domains(db):
    add_company(db, "Yandex", "7700000001", "sales@ya.ru")
    add_company(db, "Google", "7700000002", "sales@googlemail.com")
    add_company(db, "Mail", "7700000003", "sales@bk.ru")
    add_company(db, "Rambler", "7700000004", "sales@rambler.ru")
    add_company(db, "Corporate", "7700000005", "sales@example.org")
    add_company(db, "Mixed", "7700000006", "sales@yandex.ru", "office@example.com")
    add_company(db, "No email", "7700000007")

    assert company_names(db, "yandex") == {"Yandex", "Mixed"}
    assert company_names(db, "google") == {"Google"}
    assert company_names(db, "mail_ru") == {"Mail"}
    assert company_names(db, "rambler") == {"Rambler"}
    assert company_names(db, "other") == {"Corporate", "Mixed"}


def test_email_provider_filter_rejects_unknown_value():
    with pytest.raises(ValueError, match="Email provider must be one of"):
        CompanyFilters(email_provider="unknown")


def test_company_statuses_use_simplified_pipeline():
    assert StatusUpdate(status="customer").status == "customer"
    assert CompanyFilters(status="CUSTOMER").status == "customer"

    for removed in ("checked", "ready", "error"):
        with pytest.raises(ValueError, match="Status must be one of"):
            StatusUpdate(status=removed)
        with pytest.raises(ValueError, match="Status must be one of"):
            CompanyFilters(status=removed)


def test_status_update_supports_customer_and_wakes_automatic_queue_for_new(db, monkeypatch):
    company = add_company(db, "Статус", "7700000010", "lead@example.ru")
    company.status = "sent"
    db.commit()
    wakeups: list[bool] = []
    monkeypatch.setattr("app.main.wake_outreach_worker", lambda: wakeups.append(True))
    settings = Settings(_env_file=None, outreach_automatic_send_enabled=True)

    customer = update_company_status(
        company.id,
        StatusUpdate(status="customer"),
        db,
        settings,
    )
    assert customer["status"] == "customer"
    assert customer["history"][0]["description"] == (
        "Статус изменён: Письмо отправлено → Работает с нами"
    )
    assert wakeups == []

    new = update_company_status(
        company.id,
        StatusUpdate(status="new"),
        db,
        settings,
    )
    assert new["status"] == "new"
    assert wakeups == [True]
