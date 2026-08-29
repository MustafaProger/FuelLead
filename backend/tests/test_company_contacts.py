import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.config import Settings
from app.main import (
    add_company_contact,
    delete_company,
    delete_company_contact,
)
from app.models import ActivityHistory, Company, CompanyContact, ExcludedCompany
from app.schemas import ContactCreate
from app.services.checko import CompanyPayload, OkvedItem
from app.services.contacts import contact_href, normalize_contact_value, normalize_phone
from app.services.discovery import upsert_company


def make_company(db) -> Company:
    company, _ = upsert_company(
        db,
        CompanyPayload(
            name='ООО "КОНТАКТ"',
            inn="7707654321",
            ogrn="1267700000001",
            primary_okved=OkvedItem("49.41", "Грузовой транспорт"),
            emails=["info@example.ru"],
            phone_numbers=["+74951234567"],
        ),
    )
    db.commit()
    return company


def test_contact_normalization_and_links():
    assert normalize_phone("8 (999) 123-45-67") == "+79991234567"
    assert normalize_contact_value("whatsapp", "https://wa.me/79991234567") == "+79991234567"
    assert normalize_contact_value("telegram", "https://t.me/company_name") == "@company_name"
    assert normalize_contact_value("telegram", "@Company_Name") == "@company_name"
    assert contact_href("whatsapp", "+79991234567") == "https://wa.me/79991234567"
    assert contact_href("telegram", "@company_name") == "https://t.me/company_name"

    with pytest.raises(ValueError, match="кодом страны"):
        normalize_contact_value("phone", "123")
    with pytest.raises(ValueError, match="Telegram username"):
        normalize_contact_value("telegram", "bad name")


def test_manual_contacts_can_be_added_and_removed(db):
    company = make_company(db)
    settings = Settings(_env_file=None)

    payload = add_company_contact(
        company.id,
        ContactCreate(contact_type="telegram", value="@company_name"),
        db,
        settings,
    )
    manual = next(item for item in payload["contacts"] if item["contact_type"] == "telegram")
    assert manual["value"] == "@company_name"
    assert manual["href"] == "https://t.me/company_name"
    assert payload["history"][0]["event_type"] == "contact_added"

    with pytest.raises(HTTPException) as duplicate:
        add_company_contact(
            company.id,
            ContactCreate(contact_type="telegram", value="https://t.me/company_name"),
            db,
            settings,
        )
    assert duplicate.value.status_code == 409

    updated = delete_company_contact(company.id, manual["id"], db, settings)
    assert all(item["id"] != manual["id"] for item in updated["contacts"])
    assert updated["history"][0]["event_type"] == "contact_deleted"
    assert db.scalar(
        select(func.count(ActivityHistory.id)).where(
            ActivityHistory.event_type.in_(("contact_added", "contact_deleted"))
        )
    ) == 2


def test_provider_contact_cannot_be_removed_manually(db):
    company = make_company(db)
    provider_contact = db.scalar(
        select(CompanyContact).where(CompanyContact.company_id == company.id)
    )

    with pytest.raises(HTTPException) as caught:
        delete_company_contact(company.id, provider_contact.id, db, Settings(_env_file=None))

    assert caught.value.status_code == 422


def test_company_delete_cascades_data_and_adds_exclusion(db):
    company = make_company(db)
    company_id = company.id

    result = delete_company(company_id, db)

    assert result["deleted"] is True
    assert result["excluded_from_discovery"] is True
    assert db.get(Company, company_id) is None
    assert db.scalar(select(func.count(CompanyContact.id))) == 0
    excluded = db.scalar(select(ExcludedCompany))
    assert excluded.inn == "7707654321"
    assert excluded.name == 'ООО "КОНТАКТ"'
