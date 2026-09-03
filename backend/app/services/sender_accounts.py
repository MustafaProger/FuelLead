from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import OutreachCampaign, OutreachDelivery, SenderAccount
from app.schemas import SenderAccountCreate, SenderAccountUpdate
from app.services.credentials import CredentialCipher, CredentialEncryptionError
from app.services.imap_bounces import IMAPCollectorError, MailruIMAPClient
from app.services.smtp import MailruSMTPClient, SMTPAccepted, SMTPDeliveryError


MAILRU_SMTP_HOST = "smtp.mail.ru"
MAILRU_SMTP_PORT = 465
MAILRU_IMAP_HOST = "imap.mail.ru"
MAILRU_IMAP_PORT = 993
ACTIVE_CAMPAIGN_STATUSES = ("running", "paused", "cooldown", "interrupted")


class SenderAccountError(RuntimeError):
    pass


def batch_size_for_successes(successful_full_batches: int) -> int:
    return min(12, 5 + max(0, successful_full_batches) // 2)


def sender_account_to_dict(account: SenderAccount) -> dict:
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
        "verification_checked_at": account.verification_checked_at.isoformat()
        if account.verification_checked_at
        else None,
        "daily_limit": account.daily_limit,
        "sent_today": account.sent_today,
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
    account = SenderAccount(
        provider="mailru_smtp",
        email=data.email,
        display_name=data.display_name.strip(),
        encrypted_password=cipher.encrypt(data.password),
        smtp_host=MAILRU_SMTP_HOST,
        smtp_port=MAILRU_SMTP_PORT,
        imap_host=MAILRU_IMAP_HOST,
        imap_port=MAILRU_IMAP_PORT,
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
    if data.is_active is not None:
        account.is_active = data.is_active
    if changed_credentials:
        account.verification_status = "unverified"
        account.verification_error = None
        account.verification_checked_at = None
    account.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(account)
    return account


def _password(account: SenderAccount, settings: Settings) -> str:
    return CredentialCipher(settings.mail_credentials_encryption_key).decrypt(
        account.encrypted_password
    )


def verify_sender_account(
    db: Session,
    account: SenderAccount,
    settings: Settings,
    *,
    smtp_client_factory: Callable = MailruSMTPClient,
    imap_client_factory: Callable = MailruIMAPClient,
) -> SenderAccount:
    timestamp = datetime.now(timezone.utc)
    try:
        password = _password(account, settings)
        smtp_client_factory(
            account,
            password,
            timeout_seconds=settings.mail_smtp_timeout_seconds,
        ).verify()
        if account.imap_enabled:
            with imap_client_factory(
                account,
                password,
                timeout_seconds=settings.mail_imap_timeout_seconds,
            ):
                pass
    except SMTPDeliveryError as exc:
        account.verification_status = (
            "blocked" if exc.category == "auth" else "temporary_error"
            if exc.category in ("timeout", "temporary", "connection", "tls")
            else "failed"
        )
        account.verification_error = exc.safe_message
    except IMAPCollectorError:
        account.verification_status = "failed"
        account.verification_error = "SMTP работает, но IMAP Mail.ru не прошёл проверку"
    except CredentialEncryptionError as exc:
        account.verification_status = "failed"
        account.verification_error = str(exc)
    else:
        account.verification_status = "verified"
        account.verification_error = None
        account.block_reason = None
    account.verification_checked_at = timestamp
    account.updated_at = timestamp
    db.commit()
    db.refresh(account)
    return account


def send_test_message(
    account: SenderAccount,
    recipient: str,
    settings: Settings,
    *,
    smtp_client_factory: Callable = MailruSMTPClient,
) -> SMTPAccepted:
    if not account.is_active or not account.smtp_enabled:
        raise SenderAccountError("Ящик приостановлен или SMTP отключён")
    password = _password(account, settings)
    client = smtp_client_factory(
        account,
        password,
        timeout_seconds=settings.mail_smtp_timeout_seconds,
    )
    return client.send(
        recipient,
        "FuelLead — проверка Mail.ru SMTP",
        "Это одиночное тестовое письмо FuelLead. Рассылка компаниям не запускалась.",
    )


def sender_used_by_active_campaign(db: Session, account_id: int) -> bool:
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
