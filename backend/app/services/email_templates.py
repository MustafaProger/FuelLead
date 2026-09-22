import re
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from datetime import date, datetime, timezone

import nh3
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Company, EmailTemplate
from app.services.email_attachments import attachment_metadata, get_attachments
from app.services.email_branding import allow_email_image, artel_letterhead


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


_HTML_CLEANER = nh3.Cleaner(
    tags={"a", "p", "br", "div", "span", "strong", "b", "em", "i", "u", "s", "h1", "h2", "h3", "h4",
          "ul", "ol", "li", "table", "thead", "tbody", "tfoot", "tr", "td", "th", "hr", "img", "blockquote"},
    clean_content_tags={"script", "style", "iframe", "object", "svg", "math", "form", "head", "title"},
    attributes={"*": {"style", "align", "dir", "lang"}, "a": {"href", "title"},
                "img": {"src", "alt", "width", "height"},
                "table": {"width", "cellpadding", "cellspacing", "border", "role", "bgcolor"},
                "td": {"width", "colspan", "rowspan", "valign", "bgcolor"},
                "th": {"width", "colspan", "rowspan", "valign", "bgcolor"}},
    url_schemes={"https", "http", "mailto", "tel", "data"},
    attribute_filter=allow_email_image,
    url_relative="deny",
    filter_style_properties={"color", "background-color", "font-family", "font-size", "font-weight",
        "font-style", "line-height", "letter-spacing", "text-align", "text-decoration", "vertical-align",
        "padding", "padding-top", "padding-bottom", "padding-left", "padding-right", "margin",
        "margin-top", "margin-bottom", "margin-left", "margin-right", "border", "border-top", "border-bottom",
        "border-left", "border-right", "border-color", "border-radius", "border-collapse", "border-spacing",
        "width", "max-width", "min-width", "height", "max-height", "word-break", "overflow-wrap"},
)


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"p", "div", "br", "tr", "h1", "h2", "h3", "h4", "blockquote", "hr"}:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("\n• ")
        if tag in {"td", "th"}:
            self.parts.append(" ")
        if tag == "img":
            self.parts.append(dict(attrs).get("alt", ""))

    def handle_endtag(self, tag):
        if tag in {"p", "div", "tr", "h1", "h2", "h3", "h4", "li", "blockquote"}:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(re.sub(r"\s+", " ", data))


def html_to_text(html: str) -> str:
    parser = _PlainText()
    parser.feed(html)
    text = "\n".join(line.strip() for line in "".join(parser.parts).splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def append_opt_out_footer(body: str, settings: Settings) -> str:
    footer, body = settings.outreach_opt_out_text.strip(), body.strip()
    return f"{body}\n\n—\n{footer}" if footer and footer not in body else body


def render_email_content(body_format: str, body_template: str, html_template: str,
                         values: dict[str, str], settings: Settings) -> tuple[str, str | None]:
    if body_format == "html":
        # Escape substitutions before parsing: company data must never become markup.
        rendered = render_email_template(html_template, {key: escape(value, quote=True) for key, value in values.items()})
        html = _HTML_CLEANER.clean(rendered)
        plain = html_to_text(html)
        if not plain:
            raise ValueError("HTML-письмо должно содержать видимый текст")
        footer = settings.outreach_opt_out_text.strip()
        if footer and footer not in plain:
            html += f'<p style="font:12px Arial,sans-serif;color:#667085;padding:16px">{escape(footer)}</p>'
        return append_opt_out_footer(plain, settings), html
    body = render_email_template(body_template, values).strip()
    if not body:
        raise ValueError("Текст письма обязателен")
    return append_opt_out_footer(body, settings), None


def artel_offer_preset() -> dict:
    directory = Path(__file__).resolve().parent.parent / "templates"
    return {"subject_template": "Топливные карты ЛУКОЙЛ и Teboil для {{company_name}}",
            "body_template": (directory / "artel_offer.txt").read_text(encoding="utf-8"),
            "html_template": (directory / "artel_offer.html").read_text(encoding="utf-8").replace(
                "__ARTEL_LETTERHEAD_DATA_URL__", artel_letterhead().data_url),
            "body_format": "html"}


def email_template_to_dict(template: EmailTemplate, settings: Settings, db: Session) -> dict:
    updated_at = template.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return {
        "id": template.id,
        "name": template.name,
        "subject_template": template.subject_template,
        "body_template": template.body_template,
        "body_format": template.body_format,
        "html_template": template.html_template,
        "attachments": [attachment_metadata(file) for file in get_attachments(db, template.attachment_ids)],
        "updated_at": updated_at.astimezone(settings.timezone).isoformat(),
        "variables": list(TEMPLATE_VARIABLES),
    }
