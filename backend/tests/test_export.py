from io import BytesIO

from openpyxl import load_workbook

from app.config import Settings
from app.export import build_xlsx
from app.models import CompanyContact
from app.services.checko import CompanyPayload, OkvedItem
from app.services.discovery import upsert_company


def test_xlsx_export_contains_company_and_multiple_emails(db):
    company, _ = upsert_company(
        db,
        CompanyPayload(
            name='ООО "ЭКСПОРТ"',
            inn="7707654321",
            ogrn="1267700000001",
            primary_okved=OkvedItem("42.11", "Строительство дорог"),
            additional_okveds=[OkvedItem("43.12.3", "Земляные работы")],
            emails=["one@example.ru", "two@example.ru"],
        ),
    )
    db.commit()
    company.contacts.extend(
        [
            CompanyContact(contact_type="phone", value="+74951234567", source="Checko API"),
            CompanyContact(contact_type="whatsapp", value="+79991234567", source="Вручную"),
            CompanyContact(contact_type="telegram", value="@export_company", source="Вручную"),
        ]
    )
    db.commit()

    content = build_xlsx([company], Settings())
    workbook = load_workbook(BytesIO(content))
    sheet = workbook["FuelLead"]

    assert sheet["A2"].value == 'ООО "ЭКСПОРТ"'
    assert sheet["B2"].value == "7707654321"
    assert "one@example.ru" in sheet["F2"].value
    assert "two@example.ru" in sheet["F2"].value
    assert sheet["H2"].value == "+74951234567"
    assert sheet["I2"].value == "+79991234567"
    assert sheet["J2"].value == "@export_company"
