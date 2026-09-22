import re
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from app.mail_providers import MAIL_PROVIDERS
from app.config import DEFAULT_OKVED_CODES
from app.email_providers import EMAIL_PROVIDER_VALUES
from app.models import ALL_STATUSES, CONTACT_TYPES
from app.services.provider import normalize_email


class AuthLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class CompanyFilters(BaseModel):
    status: str | None = None
    has_email: bool | None = None
    email_provider: str | None = None
    category: str | None = None
    discovered_on: date | None = None
    search: str | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().lower()
        if normalized not in ALL_STATUSES:
            raise ValueError(f"Status must be one of: {', '.join(ALL_STATUSES)}")
        return normalized

    @field_validator("email_provider")
    @classmethod
    def validate_email_provider(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().lower()
        if normalized not in EMAIL_PROVIDER_VALUES:
            raise ValueError(f"Email provider must be one of: {', '.join(EMAIL_PROVIDER_VALUES)}")
        return normalized


class StatusUpdate(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in ALL_STATUSES:
            raise ValueError(f"Status must be one of: {', '.join(ALL_STATUSES)}")
        return value


class ContactCreate(BaseModel):
    contact_type: str
    value: str = Field(min_length=1, max_length=320)

    @field_validator("contact_type")
    @classmethod
    def validate_contact_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in CONTACT_TYPES:
            raise ValueError(f"Contact type must be one of: {', '.join(CONTACT_TYPES)}")
        return normalized


class SearchRunCreate(BaseModel):
    search_scope: Literal["full", "batch"] = "full"
    okved_codes: list[str] = Field(default_factory=lambda: DEFAULT_OKVED_CODES.copy(), min_length=1, max_length=30)
    limit_per_code: int = Field(default=10, ge=1, le=100)

    @field_validator("okved_codes")
    @classmethod
    def clean_codes(cls, value: list[str]) -> list[str]:
        codes = list(dict.fromkeys(code.strip() for code in value if code.strip()))
        if not codes:
            raise ValueError("At least one OKVED code is required")
        return codes


class EmailTemplateUpdate(BaseModel):
    subject_template: str = Field(min_length=1, max_length=998)
    body_template: str = Field(default="", max_length=20_000)
    body_format: Literal["text", "html"] = "text"
    html_template: str = Field(default="", max_length=200_000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("subject_template")
    @classmethod
    def strip_template(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Template field cannot be empty")
        return value

    @model_validator(mode="after")
    def validate_content(self):
        if "\r" in self.subject_template or "\n" in self.subject_template:
            raise ValueError("Тема должна занимать одну строку")
        if not (self.html_template if self.body_format == "html" else self.body_template).strip():
            raise ValueError("Заполните содержимое выбранного формата письма")
        return self


class EmailPreviewRequest(BaseModel):
    company_id: int | None = Field(default=None, ge=1)
    subject_template: str | None = Field(default=None, max_length=998)
    body_template: str | None = Field(default=None, max_length=20_000)
    body_format: Literal["text", "html"] | None = None
    html_template: str | None = Field(default=None, max_length=200_000)
    attachment_ids: list[str] | None = Field(default=None, max_length=5)
    recipient: str | None = Field(default=None, max_length=320)


class EmailSendRequest(BaseModel):
    recipient: str | None = Field(default=None, max_length=320)
    subject: str | None = Field(default=None, max_length=998)
    body: str | None = Field(default=None, max_length=20_000)


class OutreachPreflightRequest(BaseModel):
    filters: CompanyFilters = Field(default_factory=CompanyFilters)


class OutreachCampaignCreate(BaseModel):
    snapshot_id: int = Field(ge=1)
    confirmed: bool

    @field_validator("confirmed")
    @classmethod
    def require_confirmation(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Подтвердите проверку получателей перед запуском")
        return value


def normalize_app_password(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    # Google displays its 16-letter app passwords in four groups.
    if re.fullmatch(r"[A-Za-z]{4}( [A-Za-z]{4}){3}", value):
        value = value.replace(" ", "")
    if not value or not value.isascii() or any(char.isspace() or not char.isprintable() for char in value):
        raise ValueError("Пароль приложения должен содержать только латинские символы без пробелов внутри. Скопируйте его из настроек почтового сервиса")
    return value


class SenderAccountCreate(BaseModel):
    provider: str = "mailru_smtp"
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(default="", max_length=200)
    password: str = Field(min_length=1, max_length=512, repr=False)
    daily_limit: int = Field(default=50, ge=1, le=500)
    smtp_enabled: bool = True
    imap_enabled: bool = False

    _password_format = field_validator("password")(normalize_app_password)

    @field_validator("provider")
    @classmethod
    def supported_provider(cls, value: str) -> str:
        if value not in MAIL_PROVIDERS:
            raise ValueError("Выберите Mail.ru, Gmail или Яндекс Почту")
        return value

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str, info: ValidationInfo) -> str:
        normalized = normalize_email(value)
        if not normalized:
            raise ValueError("Укажите полный корректный адрес почтового ящика")
        preset = MAIL_PROVIDERS.get(info.data.get("provider", "mailru_smtp"))
        if preset and normalized.rsplit("@", 1)[1] not in preset.domains:
            raise ValueError(f"Укажите полный адрес ящика {preset.label}")
        return normalized


class SenderAccountUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    password: str | None = Field(default=None, min_length=1, max_length=512, repr=False)
    daily_limit: int | None = Field(default=None, ge=1, le=500)
    smtp_enabled: bool | None = None
    imap_enabled: bool | None = None
    is_active: bool | None = None

    _password_format = field_validator("password")(normalize_app_password)


class SenderTestEmailRequest(BaseModel):
    recipient: str = Field(min_length=3, max_length=320)
    confirmed: bool
    subject: str | None = Field(default=None, min_length=1, max_length=998)
    body: str | None = Field(default=None, min_length=1, max_length=20_000)

    @field_validator("recipient")
    @classmethod
    def valid_recipient(cls, value: str) -> str:
        normalized = normalize_email(value)
        if not normalized:
            raise ValueError("Укажите корректный адрес получателя")
        return normalized

    @field_validator("confirmed")
    @classmethod
    def require_test_confirmation(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Подтвердите адрес тестового письма")
        return value


class EmailSuppressionCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    reason: str = Field(min_length=1, max_length=500)
    comment: str | None = Field(default=None, max_length=2000)

    @field_validator("email")
    @classmethod
    def valid_suppression_email(cls, value: str) -> str:
        normalized = normalize_email(value)
        if not normalized:
            raise ValueError("Укажите корректный email")
        return normalized


class EmailSuppressionLift(BaseModel):
    confirmed: bool
    comment: str = Field(min_length=3, max_length=2000)

    @field_validator("confirmed")
    @classmethod
    def require_lift_confirmation(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Подтвердите снятие исключения")
        return value


class UncertainDeliveryResolution(BaseModel):
    outcome: str
    confirmed: bool

    @field_validator("outcome")
    @classmethod
    def valid_outcome(cls, value: str) -> str:
        if value not in ("accepted", "failed"):
            raise ValueError("Результат должен быть accepted или failed")
        return value

    @field_validator("confirmed")
    @classmethod
    def require_resolution_confirmation(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Подтвердите ручное решение")
        return value


class ConversationReplyRequest(BaseModel):
    request_id: UUID
    reply_id: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=20_000)

    @field_validator("body")
    @classmethod
    def nonempty_body(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Напишите ответ")
        return value.strip()


class ConversationReadRequest(BaseModel):
    through_id: int = Field(ge=1)
