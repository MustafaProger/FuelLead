from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


ALL_STATUSES = (
    "new",
    "sent",
    "answered",
    "interested",
    "customer",
    "rejected",
    "error",
)
REMOVED_COMPANY_STATUSES = ("checked", "ready")
COMPANY_STATUS_LABELS = {
    "new": "Новая",
    "sent": "Письмо отправлено",
    "answered": "Ответила",
    "interested": "Заинтересована",
    "customer": "Работает с нами",
    "rejected": "Отказ",
    "error": "Ошибка отправки",
}
CONTACT_TYPES = ("phone", "whatsapp", "telegram")
OUTREACH_CAMPAIGN_STATUSES = ("running", "paused", "completed", "cancelled")
OUTREACH_DELIVERY_STATUSES = ("queued", "sending", "sent", "failed", "cancelled")


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint(
            "status IN ('new','sent','answered','interested','customer','rejected','error')",
            name="ck_companies_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(400), nullable=False)
    inn: Mapped[str] = mapped_column(String(12), nullable=False, unique=True, index=True)
    ogrn: Mapped[str | None] = mapped_column(String(15), index=True)
    primary_okved_code: Mapped[str | None] = mapped_column(String(20), index=True)
    primary_okved_name: Mapped[str | None] = mapped_column(String(500))
    activity_category: Mapped[str] = mapped_column(String(40), default="other", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="new", nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(30), default="checko", nullable=False)
    first_discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    last_checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    emails: Mapped[list["CompanyEmail"]] = relationship(
        back_populates="company", cascade="all, delete-orphan", lazy="selectin"
    )
    contacts: Mapped[list["CompanyContact"]] = relationship(
        back_populates="company", cascade="all, delete-orphan", lazy="selectin"
    )
    additional_okveds: Mapped[list["CompanyOkved"]] = relationship(
        back_populates="company", cascade="all, delete-orphan", lazy="selectin"
    )
    history: Mapped[list["ActivityHistory"]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="desc(ActivityHistory.created_at)",
    )


class CompanyEmail(Base):
    __tablename__ = "company_emails"
    __table_args__ = (UniqueConstraint("company_id", "email", name="uq_company_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(100), default="Checko API", nullable=False)
    first_discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    company: Mapped[Company] = relationship(back_populates="emails")


class CompanyContact(Base):
    __tablename__ = "company_contacts"
    __table_args__ = (
        UniqueConstraint("company_id", "contact_type", "value", name="uq_company_contact"),
        CheckConstraint(
            "contact_type IN ('phone','whatsapp','telegram')",
            name="ck_company_contacts_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    contact_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    value: Mapped[str] = mapped_column(String(320), nullable=False)
    source: Mapped[str] = mapped_column(String(100), default="Вручную", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    company: Mapped[Company] = relationship(back_populates="contacts")


class ExcludedCompany(Base):
    __tablename__ = "excluded_companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inn: Mapped[str] = mapped_column(String(12), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(400), nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class CompanyOkved(Base):
    __tablename__ = "company_okveds"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_company_okved"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(500))

    company: Mapped[Company] = relationship(back_populates="additional_okveds")


class ActivityHistory(Base):
    __tablename__ = "activity_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str | None] = mapped_column(String(20))
    event_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    company: Mapped[Company] = relationship(back_populates="history")


class SearchRun(Base):
    __tablename__ = "search_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(20), default="checko", nullable=False)
    requested_okved_codes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    candidates_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    companies_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    companies_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_inactive: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    errors_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DiscoveryCursor(Base):
    __tablename__ = "discovery_cursors"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "okved_code",
            "region_code",
            name="uq_discovery_cursor_provider_query",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Nullable keeps direct imports of legacy cursor rows compatible. All cursors
    # created by discovery are explicitly scoped to a provider.
    provider: Mapped[str | None] = mapped_column(String(20), index=True)
    okved_code: Mapped[str] = mapped_column(String(20), nullable=False)
    region_code: Mapped[str] = mapped_column(String(2), nullable=False)
    next_page: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    next_record_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    page_size: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    completed_cycles: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class EmailTemplate(Base):
    __tablename__ = "email_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), default="Основной шаблон", nullable=False)
    subject_template: Mapped[str] = mapped_column(Text, nullable=False)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class OutreachCampaign(Base):
    __tablename__ = "outreach_campaigns"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','paused','completed','cancelled')",
            name="ck_outreach_campaigns_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False, index=True)
    filters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    matched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recipient_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cancelled_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    hourly_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    min_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_per_domain_per_day: Mapped[int] = mapped_column(Integer, nullable=False)
    pause_reason: Mapped[str | None] = mapped_column(Text)
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    deliveries: Mapped[list["OutreachDelivery"]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="OutreachDelivery.id",
    )


class OutreachDelivery(Base):
    __tablename__ = "outreach_deliveries"
    __table_args__ = (
        UniqueConstraint("campaign_id", "recipient", name="uq_outreach_campaign_recipient"),
        CheckConstraint(
            "status IN ('queued','sending','sent','failed','cancelled')",
            name="ck_outreach_deliveries_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("outreach_campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    company_name: Mapped[str] = mapped_column(String(400), nullable=False)
    company_inn: Mapped[str] = mapped_column(String(12), nullable=False)
    recipient: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    recipient_domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False, index=True)
    message_id: Mapped[str | None] = mapped_column(String(255), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    campaign: Mapped[OutreachCampaign] = relationship(back_populates="deliveries")
