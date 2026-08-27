import re
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Company, EmailTemplate


DEFAULT_SUBJECT_TEMPLATE = "Топливные карты для {{company_name}}"
DEFAULT_BODY_TEMPLATE = """Добрый день, {{company_name}}!

Предлагаем обсудить условия по топливным картам для вашей компании.

Дата предложения: {{date}}

С уважением,
команда FuelLead"""

TEMPLATE_VARIABLES = (
    {"key": "company_name", "token": "{{company_name}}", "label": "Название компании"},
    {"key": "date", "token": "{{date}}", "label": "Сегодняшняя дата"},
    {"key": "inn", "token": "{{inn}}", "label": "ИНН"},
    {"key": "primary_okved", "token": "{{primary_okved}}", "label": "Основной ОКВЭД"},
    {"key": "email", "token": "{{email}}", "label": "Email получателя"},
)

_TOKEN_PATTERN = re.compile(r"{{\s*([a-z_]+)\s*}}")
_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def get_or_create_email_template(db: Session) -> EmailTemplate:
    template = db.get(EmailTemplate, 1)
    if template:
        return template
    template = EmailTemplate(
        id=1,
        name="Основной шаблон",
        subject_template=DEFAULT_SUBJECT_TEMPLATE,
        body_template=DEFAULT_BODY_TEMPLATE,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


def format_russian_date(value: date) -> str:
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year} г."


def company_template_values(
    company: Company,
    recipient: str,
    settings: Settings,
    *,
    today: date | None = None,
) -> dict[str, str]:
    local_today = today or datetime.now(settings.timezone).date()
    okved_parts = [part for part in (company.primary_okved_code, company.primary_okved_name) if part]
    return {
        "company_name": company.name,
        "date": format_russian_date(local_today),
        "inn": company.inn,
        "primary_okved": " — ".join(okved_parts) or "не указан",
        "email": recipient,
    }


def render_email_template(template: str, values: dict[str, str]) -> str:
    unknown = sorted({match.group(1) for match in _TOKEN_PATTERN.finditer(template)} - values.keys())
    if unknown:
        tokens = ", ".join(f"{{{{{key}}}}}" for key in unknown)
        raise ValueError(f"Неизвестные переменные шаблона: {tokens}")
    return _TOKEN_PATTERN.sub(lambda match: values[match.group(1)], template)


def email_template_to_dict(template: EmailTemplate, settings: Settings) -> dict:
    updated_at = template.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return {
        "id": template.id,
        "name": template.name,
        "subject_template": template.subject_template,
        "body_template": template.body_template,
        "updated_at": updated_at.astimezone(settings.timezone).isoformat(),
        "variables": list(TEMPLATE_VARIABLES),
    }
