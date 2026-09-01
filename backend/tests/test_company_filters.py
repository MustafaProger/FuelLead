import pytest

from app.models import Company, CompanyEmail
from app.queries import build_company_query
from app.schemas import CompanyFilters


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
