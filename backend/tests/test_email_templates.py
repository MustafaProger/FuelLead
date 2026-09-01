from datetime import date

import pytest

from app import main
from app.config import Settings
from app.models import Company, CompanyEmail
from app.schemas import EmailSendRequest
from app.services.email_templates import (
    company_template_values,
    get_or_create_email_template,
    render_email_template,
)


def make_company(db) -> Company:
    company = Company(
        name='ООО "АРТЕЛЬ"',
        inn="7701234567",
        ogrn="1267700000001",
        primary_okved_code="49.41",
        primary_okved_name="Грузовые перевозки",
        activity_category="freight",
        status="ready",
    )
    company.emails.append(CompanyEmail(email="office@artel.ru", source="Checko API"))
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


def test_template_renders_company_values_and_russian_date(db):
    company = make_company(db)
    values = company_template_values(
        company,
        "office@artel.ru",
        Settings(_env_file=None),
        today=date(2026, 8, 27),
    )

    rendered = render_email_template(
        "Добрый день, {{company_name}}! {{date}} · {{inn}} · {{primary_okved}} · {{email}}",
        values,
    )

    assert 'Добрый день, ООО "АРТЕЛЬ"!' in rendered
    assert "27 августа 2026 г." in rendered
    assert "7701234567" in rendered
    assert "49.41 — Грузовые перевозки" in rendered
    assert "office@artel.ru" in rendered


def test_template_rejects_unknown_variables():
    with pytest.raises(ValueError, match=r"\{\{manager_name\}\}"):
        render_email_template("{{manager_name}}", {"company_name": "ООО Артель"})


def test_default_template_is_persisted_once(db):
    first = get_or_create_email_template(db)
    second = get_or_create_email_template(db)

    assert first.id == second.id == 1
    assert "{{company_name}}" in first.subject_template
    assert "{{date}}" in first.body_template


def test_single_company_send_updates_status_and_history(db, monkeypatch):
    company = make_company(db)

    class FakeSender:
        def __init__(self, config):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def send(self, recipient: str, subject: str, body: str) -> str:
            assert recipient == "office@artel.ru"
            assert subject == 'Предложение для ООО "АРТЕЛЬ"'
            assert body.startswith("Персональный текст")
            assert "ответьте «Не писать»" in body
            return "message-123"

    monkeypatch.setattr(main, "GmailOAuthSender", FakeSender)
    settings = Settings(
        _env_file=None,
        outreach_sender_email="sender@example.ru",
        gmail_client_id="client-id",
        gmail_client_secret="client-secret",
        gmail_refresh_token="refresh-token",
    )

    result = main.send_company_email(
        company.id,
        EmailSendRequest(
            recipient="office@artel.ru",
            subject="Предложение для {{company_name}}",
            body="Персональный текст",
        ),
        db,
        settings,
    )

    assert result["message_id"] == "message-123"
    assert company.status == "sent"
    assert company.history[0].event_type == "email_sent"
    assert company.history[0].event_data["recipient"] == "office@artel.ru"


def test_company_send_without_recipient_uses_only_primary_company_email(db, monkeypatch):
    company = make_company(db)
    company.emails.append(CompanyEmail(email="sales@artel.ru", source="Checko API"))
    db.commit()

    class FakeSender:
        sent: list[tuple[str, str, str]] = []

        def __init__(self, config):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def send(self, recipient: str, subject: str, body: str) -> str:
            self.__class__.sent.append((recipient, subject, body))
            return f"message-{recipient}"

    monkeypatch.setattr(main, "GmailOAuthSender", FakeSender)
    settings = Settings(
        _env_file=None,
        outreach_sender_email="sender@example.ru",
        gmail_client_id="client-id",
        gmail_client_secret="client-secret",
        gmail_refresh_token="refresh-token",
    )

    result = main.send_company_email(company.id, EmailSendRequest(), db, settings)

    assert [item[0] for item in FakeSender.sent] == ["office@artel.ru"]
    assert result["recipients"] == ["office@artel.ru"]
    assert result["sent_count"] == 1
    assert len(company.history) == 1
    assert company.history[0].event_data["recipient"] == "office@artel.ru"
