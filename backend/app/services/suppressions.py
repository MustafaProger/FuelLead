from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import EmailSuppression
from app.services.provider import normalize_email


def active_suppressed_addresses(db: Session) -> set[str]:
    return {
        value
        for value in db.scalars(
            select(EmailSuppression.email).where(EmailSuppression.lifted_at.is_(None))
        ).all()
        if value
    }


def is_suppressed(db: Session, email: str) -> bool:
    normalized = normalize_email(email)
    return bool(
        normalized
        and db.scalar(
            select(EmailSuppression.id).where(
                EmailSuppression.email == normalized,
                EmailSuppression.lifted_at.is_(None),
            )
        )
    )


def add_or_restore_suppression(
    db: Session,
    email: str,
    reason: str,
    *,
    source: str,
    campaign_id: int | None = None,
    delivery_id: int | None = None,
    smtp_code: str | None = None,
    comment: str | None = None,
) -> EmailSuppression:
    normalized = normalize_email(email)
    if not normalized:
        raise ValueError("Некорректный email")
    suppression = db.scalar(
        select(EmailSuppression).where(EmailSuppression.email == normalized)
    )
    if suppression is None:
        suppression = EmailSuppression(
            email=normalized,
            reason=reason,
            source=source,
        )
        db.add(suppression)
    suppression.reason = reason
    suppression.source = source
    suppression.campaign_id = campaign_id
    suppression.delivery_id = delivery_id
    suppression.smtp_code = smtp_code
    suppression.comment = comment
    suppression.created_at = datetime.now(timezone.utc)
    suppression.lifted_at = None
    db.commit()
    db.refresh(suppression)
    return suppression


def list_suppressions(db: Session, search: str = "") -> list[EmailSuppression]:
    query = select(EmailSuppression)
    clean = search.strip().lower()
    if clean:
        pattern = f"%{clean}%"
        query = query.where(
            or_(
                EmailSuppression.email.ilike(pattern),
                EmailSuppression.reason.ilike(pattern),
                EmailSuppression.comment.ilike(pattern),
            )
        )
    return list(
        db.scalars(
            query.order_by(
                EmailSuppression.lifted_at.is_not(None),
                EmailSuppression.created_at.desc(),
                EmailSuppression.id.desc(),
            )
        ).all()
    )


def suppression_to_dict(item: EmailSuppression) -> dict:
    return {
        "id": item.id,
        "email": item.email,
        "reason": item.reason,
        "source": item.source,
        "campaign_id": item.campaign_id,
        "delivery_id": item.delivery_id,
        "smtp_code": item.smtp_code,
        "created_at": item.created_at.isoformat(),
        "lifted_at": item.lifted_at.isoformat() if item.lifted_at else None,
        "comment": item.comment,
        "active": item.lifted_at is None,
    }
