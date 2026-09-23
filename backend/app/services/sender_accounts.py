import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.mail_providers import MAIL_PROVIDERS, SMTP_SENDER_PROVIDERS
from app.config import Settings, get_settings
from app.models import EmailReplyAttempt, OutreachCampaign, OutreachDelivery, SenderAccount
from app.schemas import SenderAccountCreate, SenderAccountUpdate
from app.services.credentials import CredentialCipher, CredentialEncryptionError
from app.services.imap_bounces import IMAPCollectorError, MailruIMAPClient, imap_failure_status
from app.services.smtp import MailruSMTPClient, SMTPAccepted, SMTPDeliveryError


ACTIVE_CAMPAIGN_STATUSES = ("running", "paused", "cooldown", "interrupted")
_SMTP_HEALTH_FIELDS = ("verification_status", "verification_error", "verification_error_category", "verification_checked_at", "verification_retry_at", "blocked_until_round", "block_reason", "last_sent_at")
logger = logging.getLogger("fuellead.mail_health")


class SenderAccountError(RuntimeError):
    pass


def batch_size_for_successes(successful_full_batches: int) -> int:
    return min(12, 5 + max(0, successful_full_batches) // 2)


def sender_account_to_dict(account: SenderAccount, *, settings: Settings | None = None, now: datetime | None = None) -> dict:
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    today = timestamp.astimezone((settings or get_settings()).timezone).date()
    # Sending resets the stored counter lazily. A read must still show today's
    # usage while leaving accounting intact; undated legacy counts stay intact.
    sent_today = 0 if account.sent_today_date is not None and account.sent_today_date < today else account.sent_today
    return {
        "id": account.id,
        "provider": account.provider,
        "email": account.email,
        "display_name": account.display_name,
        "smtp_host": account.smtp_host,
        "smtp_port": account.smtp_port,
        "imap_host": account.imap_host,
        "imap_port": account.imap_port,
        "smtp_enabled": account.smtp_enabled,
        "imap_enabled": account.imap_enabled,
        "is_active": account.is_active,
        "password_saved": bool(account.encrypted_password),
        "verification_status": account.verification_status,
        "verification_error": account.verification_error,
        "verification_error_category": account.verification_error_category,
        "verification_retry_at": account.verification_retry_at.isoformat() if account.verification_retry_at else None,
        "imap_verification_status": account.imap_verification_status if account.imap_enabled else "disabled",
        "imap_verification_error": account.imap_verification_error if account.imap_enabled else None,
        "imap_verification_checked_at": account.imap_verification_checked_at.isoformat() if account.imap_verification_checked_at else None,
        "verification_checked_at": account.verification_checked_at.isoformat()
        if account.verification_checked_at
        else None,
        "daily_limit": account.daily_limit,
        "sent_today": sent_today,
        "successful_full_batches": account.successful_full_batches,
        "current_batch_size": account.current_batch_size,
        "blocked_until_round": account.blocked_until_round,
        "block_reason": account.block_reason,
        "last_sent_at": account.last_sent_at.isoformat() if account.last_sent_at else None,
        "created_at": account.created_at.isoformat(),
        "updated_at": account.updated_at.isoformat(),
    }


def create_sender_account(
    db: Session,
    data: SenderAccountCreate,
    settings: Settings,
) -> SenderAccount:
    cipher = CredentialCipher(settings.mail_credentials_encryption_key)
    preset = MAIL_PROVIDERS[data.provider]
    account = SenderAccount(
        provider=data.provider,
        email=data.email,
        display_name=data.display_name.strip(),
        encrypted_password=cipher.encrypt(data.password),
        smtp_host=preset.smtp_host,
        smtp_port=preset.smtp_port,
        imap_host=preset.imap_host,
        imap_port=preset.imap_port,
        smtp_enabled=data.smtp_enabled,
        imap_enabled=data.imap_enabled,
        is_active=True,
        verification_status="unverified",
        daily_limit=data.daily_limit,
        current_batch_size=5,
    )
    db.add(account)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise SenderAccountError("Этот почтовый ящик уже добавлен") from exc
    db.refresh(account)
    return account


def update_sender_account(
    db: Session,
    account: SenderAccount,
    data: SenderAccountUpdate,
    settings: Settings,
) -> SenderAccount:
    changed_credentials = False
    if data.display_name is not None:
        account.display_name = data.display_name.strip()
    if data.password is not None:
        cipher = CredentialCipher(settings.mail_credentials_encryption_key)
        account.encrypted_password = cipher.encrypt(data.password)
        changed_credentials = True
    if data.daily_limit is not None:
        account.daily_limit = data.daily_limit
    if data.smtp_enabled is not None:
        account.smtp_enabled = data.smtp_enabled
    if data.imap_enabled is not None:
        account.imap_enabled = data.imap_enabled
        account.imap_verification_status = "unverified"
        account.imap_verification_error = None
    if data.is_active is not None:
        account.is_active = data.is_active
    if changed_credentials:
        account.verification_status = "unverified"
        account.verification_error = None
        account.verification_checked_at = None
        account.verification_error_category = None
        account.verification_retry_at = None
        account.imap_verification_status = "unverified"
        account.imap_verification_error = None
        account.imap_verification_checked_at = None
    account.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(account)
    return account


def _password(account: SenderAccount, settings: Settings) -> str:
    return CredentialCipher(settings.mail_credentials_encryption_key).decrypt(
        account.encrypted_password
    )


def _revision(account: SenderAccount, fields: tuple[str, ...]) -> tuple:
    values = (getattr(account, field) for field in fields)
    return tuple(value.replace(tzinfo=None) if isinstance(value, datetime) else value for value in values)


def verify_sender_account(
    db: Session,
    account: SenderAccount,
    settings: Settings,
    *,
    smtp_client_factory: Callable = MailruSMTPClient,
    imap_client_factory: Callable = MailruIMAPClient,
    preserve_round_block: bool = False,
) -> SenderAccount:
    connection_fields = ("encrypted_password", "email", "smtp_host", "smtp_port", "imap_host", "imap_port", "smtp_enabled", "imap_enabled", "is_active")
    smtp_fields = _SMTP_HEALTH_FIELDS
    imap_fields = ("imap_verification_status", "imap_verification_error", "imap_verification_checked_at")
    original_connection = _revision(account, connection_fields)
    original_smtp = _revision(account, smtp_fields)
    original_imap = _revision(account, imap_fields)
    status, error, category = "verified", None, None
    imap_status, imap_error = "disabled", None
    try:
        password = _password(account, settings)
        smtp_client_factory(
            account,
            password,
            timeout_seconds=settings.mail_smtp_timeout_seconds,
        ).verify()
    except SMTPDeliveryError as exc:
        status = (
            "blocked" if exc.category == "auth" else "temporary_error"
            if exc.category in ("timeout", "temporary", "connection")
            else "failed"
        )
        error, category = exc.safe_message, exc.category
    except CredentialEncryptionError as exc:
        status, error, category = "failed", str(exc), "credentials"
    if account.imap_enabled and category != "credentials":
        try:
            with imap_client_factory(account, password, timeout_seconds=settings.mail_imap_timeout_seconds):
                pass
            imap_status = "verified"
        except IMAPCollectorError as exc:
            imap_status, imap_error = imap_failure_status(exc.category), str(exc)
    elif account.imap_enabled:
        imap_status, imap_error = "failed", error
    # Independent IMAP polling must not discard a slow SMTP check. A changed
    # password or newer SMTP outcome still wins; only hold the lock to persist.
    db.refresh(account, with_for_update=True)
    if _revision(account, connection_fields) != original_connection:
        db.commit()
        return account
    timestamp = datetime.now(timezone.utc)
    if _revision(account, smtp_fields) == original_smtp:
        previous_error = " ".join((account.verification_error or "", account.block_reason or "")).lower()
        policy_refusal = account.verification_error_category == "policy" or (
            account.verification_error_category == "provider"
            and any(reason in previous_error for reason in ("spam message rejected", "spam message discarded"))
        )
        # AUTH does not submit a message and cannot prove that a prior DATA
        # policy refusal has been lifted, including pre-policy legacy rows.
        if status != "verified" or not policy_refusal:
            account.verification_status = status
            account.verification_error = error
            account.verification_error_category = category
            account.verification_retry_at = timestamp + timedelta(minutes=5) if status == "temporary_error" else None
            if status == "verified" and not preserve_round_block:
                account.blocked_until_round = None
                account.block_reason = None
        account.verification_checked_at = timestamp
    if _revision(account, imap_fields) == original_imap:
        account.imap_verification_status = imap_status
        account.imap_verification_error = imap_error
        account.imap_verification_checked_at = timestamp if account.imap_enabled else None
    account.updated_at = timestamp
    db.commit()
    db.refresh(account)
    return account


def recover_temporary_sender_accounts(settings: Settings, *, session_factory=None, smtp_client_factory=MailruSMTPClient, imap_client_factory=MailruIMAPClient) -> None:
    """Login-only recovery, including paused campaigns; retain their round rest."""
    from app.database import SessionLocal
    factory = session_factory or SessionLocal
    now = datetime.now(timezone.utc)
    with factory() as db:
        account_ids = list(db.scalars(select(SenderAccount.id).where(
            SenderAccount.provider.in_(SMTP_SENDER_PROVIDERS),
            SenderAccount.is_active.is_(True), SenderAccount.smtp_enabled.is_(True),
            SenderAccount.verification_status == "temporary_error",
            SenderAccount.verification_error_category.in_(("connection", "timeout", "temporary")),
            or_(SenderAccount.verification_retry_at <= now,
                (SenderAccount.verification_retry_at.is_(None) & or_(SenderAccount.verification_checked_at.is_(None), SenderAccount.verification_checked_at <= now - timedelta(minutes=5)))),
        )).all())
    for account_id in account_ids:
        try:
            with factory() as db:
                account = db.get(SenderAccount, account_id)
                if not account or not account.is_active or not account.smtp_enabled or account.verification_status != "temporary_error":
                    continue
                verify_sender_account(db, account, settings,
                    smtp_client_factory=smtp_client_factory, imap_client_factory=imap_client_factory,
                    preserve_round_block=sender_used_by_active_campaign(db, account.id))
        except Exception as exc:
            # One bad mailbox/database session must not starve all later checks.
            logger.warning("mail_health_check_failed account_id=%s error_type=%s", account_id, type(exc).__name__)


def send_test_message(
    account: SenderAccount,
    recipient: str,
    settings: Settings,
    *,
    smtp_client_factory: Callable = MailruSMTPClient,
    subject: str | None = None,
    body: str | None = None,
) -> SMTPAccepted:
    if not account.is_active or not account.smtp_enabled:
        raise SenderAccountError("Ящик приостановлен или SMTP отключён")
    password = _password(account, settings)
    client = smtp_client_factory(
        account,
        password,
        timeout_seconds=settings.mail_smtp_timeout_seconds,
        imap_timeout_seconds=settings.mail_imap_timeout_seconds,
    )
    return client.send(
        recipient,
        subject or "FuelLead — проверка почты SMTP",
        body or "Это одиночное тестовое письмо FuelLead. Рассылка компаниям не запускалась.",
    )


def send_test_message_and_reconcile(
    db: Session,
    account: SenderAccount,
    recipient: str,
    settings: Settings,
    *,
    smtp_client_factory: Callable = MailruSMTPClient,
    subject: str | None = None,
    body: str | None = None,
) -> SMTPAccepted:
    connection_fields = ("encrypted_password", "provider", "email", "smtp_host", "smtp_port", "smtp_enabled", "is_active")
    original_connection = _revision(account, connection_fields)
    original_smtp = _revision(account, _SMTP_HEALTH_FIELDS)
    result = send_test_message(
        account, recipient, settings, smtp_client_factory=smtp_client_factory,
        subject=subject, body=body,
    )
    # Only actual DATA acceptance can clear an earlier policy refusal. Hold a
    # lock only while persisting; newer credentials/SMTP outcomes take priority,
    # while independent IMAP activity cannot discard this SMTP result.
    db.refresh(account, with_for_update=True)
    if _revision(account, connection_fields) == original_connection and _revision(account, _SMTP_HEALTH_FIELDS) == original_smtp:
        timestamp = datetime.now(timezone.utc)
        account.verification_status = "verified"
        account.verification_error = None
        account.verification_error_category = None
        account.verification_retry_at = None
        account.verification_checked_at = timestamp
        account.blocked_until_round = None
        account.block_reason = None
        account.updated_at = timestamp
    db.commit()
    return result


def sender_used_by_active_campaign(db: Session, account_id: int) -> bool:
    if db.scalar(select(EmailReplyAttempt.id).where(EmailReplyAttempt.sender_account_id == account_id,
                                                  EmailReplyAttempt.status.in_(("sending", "uncertain")))):
        return True
    campaigns = list(
        db.scalars(
            select(OutreachCampaign).where(
                OutreachCampaign.status.in_(ACTIVE_CAMPAIGN_STATUSES)
            )
        ).all()
    )
    if any(account_id in (campaign.sender_account_ids or []) for campaign in campaigns):
        return True
    return bool(
        db.scalar(
            select(OutreachDelivery.id).where(
                OutreachDelivery.sender_account_id == account_id,
                OutreachDelivery.status.in_(("queued", "sending")),
            ).limit(1)
        )
    )
