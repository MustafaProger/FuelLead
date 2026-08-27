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
    "checked",
    "ready",
    "sent",
    "answered",
    "interested",
    "rejected",
    "error",
)
MVP_STATUSES = ("new", "checked", "ready")


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint(
            "status IN ('new','checked','ready','sent','answered','interested','rejected','error')",
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
