import asyncio
import imaplib
import logging
import re
import socket
import ssl
import time
from contextlib import suppress
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import SessionLocal
from app.models import (
    EmailSuppression,
    ImapProcessedMessage,
    OutreachCampaign,
    OutreachDelivery,
    SenderAccount,
)
from app.services.credentials import CredentialCipher, CredentialEncryptionError
from app.services.dsn import DSNBounce, parse_permanent_dsn
from app.services.provider import normalize_email


logger = logging.getLogger("fuellead.imap")

IMAP_LIST_RE = re.compile(
    rb"^\((?P<flags>[^)]*)\)\s+(?:NIL|\"(?:\\.|[^\"])*\")\s+(?P<mailbox>.+)$",
    re.IGNORECASE,
)


class IMAPCollectorError(RuntimeError):
    def __init__(self, message: str, *, category: str = "protocol"):
        super().__init__(message)
        self.category = category


class MailruIMAPClient:
    def __init__(
        self,
        account: SenderAccount,
        password: str,
        *,
        timeout_seconds: float,
        imap_factory=imaplib.IMAP4_SSL,
        connect_attempts: int = 2,
        sleep_func=time.sleep,
    ):
        self.account = account
        self.password = password
        self.timeout_seconds = timeout_seconds
        self.imap_factory = imap_factory
        self.client = None
        self.connect_attempts = max(1, connect_attempts)
        self.sleep_func = sleep_func

    def __enter__(self):
        for attempt in range(self.connect_attempts):
            try:
                return self._connect()
            except IMAPCollectorError as exc:
                if exc.category not in ("connection", "timeout", "temporary") or attempt + 1 == self.connect_attempts:
                    raise
                self.sleep_func(attempt + 1)

    def _connect(self):
        stage = "connect"
        try:
            context = ssl.create_default_context()
            self.client = self.imap_factory(
                self.account.imap_host,
                self.account.imap_port,
                ssl_context=context,
                timeout=self.timeout_seconds,
            )
            stage = "login"
            self.client.login(self.account.email, self.password)
            stage = "select"
            status, _ = self.client.select("INBOX", readonly=True)
            if status != "OK":
                raise IMAPCollectorError("Mail.ru не открыл папку входящих сообщений")
            return self
        except Exception as exc:
            # __exit__ is not invoked when __enter__ fails. Close the socket
            # here, otherwise repeated checks leak authenticated connections.
            if self.client is not None:
                with suppress(Exception):
                    self.client.shutdown()
            self.client = None
            if isinstance(exc, IMAPCollectorError):
                raise
            if isinstance(exc, (socket.timeout, TimeoutError)):
                category, message = "timeout", "IMAP Mail.ru не ответил вовремя; SMTP проверяется отдельно"
            elif isinstance(exc, (ssl.SSLEOFError, ssl.SSLZeroReturnError)):
                category, message = "connection", "Соединение TLS с IMAP Mail.ru прервано; проверка повторится позже"
            elif isinstance(exc, ssl.SSLError):
                category, message = "tls", "Ошибка TLS-соединения с IMAP Mail.ru; проверьте сеть, сертификаты и время системы"
            elif isinstance(exc, (imaplib.IMAP4.abort, OSError)):
                category, message = "connection", "Соединение с IMAP Mail.ru прервано; SMTP проверяется отдельно"
            elif isinstance(exc, imaplib.IMAP4.error) and stage == "login":
                if any(marker in str(exc).upper() for marker in ("UNAVAILABLE", "LIMIT", "TRY AGAIN")):
                    category, message = "temporary", "Mail.ru временно ограничил IMAP-подключения; проверка повторится позже"
                else:
                    category, message = "auth", "IMAP Mail.ru отклонил вход. Проверьте доступ по IMAP и пароль приложения в Mail.ru"
            else:
                category, message = "protocol", "Не удалось выполнить IMAP-операцию Mail.ru"
            raise IMAPCollectorError(message, category=category) from exc

    def __exit__(self, *_):
        if self.client is None:
            return
        with suppress(Exception):
            self.client.close()
        with suppress(Exception):
            self.client.logout()

    def messages_after(self, last_uid: int, limit: int) -> list[tuple[int, bytes]]:
        try:
            return self._messages_after(last_uid, limit)
        except IMAPCollectorError:
            raise
        except (imaplib.IMAP4.error, OSError) as exc:
            raise IMAPCollectorError("Не удалось получить новые сообщения IMAP Mail.ru; проверка повторится позже", category="connection") from exc

    def _messages_after(self, last_uid: int, limit: int) -> list[tuple[int, bytes]]:
        assert self.client is not None
        status, values = self.client.uid("search", None, f"UID {last_uid + 1}:*")
        if status != "OK":
            raise IMAPCollectorError("Mail.ru не вернул список новых сообщений", category="temporary")
        raw_uids = values[0].split() if values and values[0] else []
        # IMAP ranges are inclusive in both directions: N:* may return the
        # last existing UID even when it is below N.
        raw_uids = [uid for uid in raw_uids if int(uid) > last_uid]
        result: list[tuple[int, bytes]] = []
        for raw_uid in raw_uids[:limit]:
            uid = int(raw_uid)
            status, payload = self.client.uid("fetch", raw_uid, "(BODY.PEEK[])")
            if status != "OK":
                continue
            raw_message = next(
                (item[1] for item in payload if isinstance(item, tuple) and isinstance(item[1], bytes)),
                None,
            )
            if raw_message:
                result.append((uid, raw_message))
        return result

    def append_sent(self, raw_message: bytes, sent_at: datetime) -> None:
        """Save an accepted SMTP message in Mail.ru's server-side Sent folder."""
        assert self.client is not None
        try:
            status, values = self.client.list()
            if status != "OK":
                raise IMAPCollectorError("Mail.ru не вернул список почтовых папок")

            sent_mailbox = None
            for value in values or []:
                if not isinstance(value, bytes):
                    continue
                match = IMAP_LIST_RE.match(value)
                if match and b"\\sent" in match.group("flags").lower().split():
                    # Keep the server-provided modified UTF-7 name and quoting.
                    sent_mailbox = match.group("mailbox")
                    break
            if sent_mailbox is None:
                raise IMAPCollectorError("Mail.ru не сообщил системную папку «Отправленные»")

            status, _ = self.client.append(sent_mailbox, r"(\Seen)", sent_at, raw_message)
            if status != "OK":
                raise IMAPCollectorError("Mail.ru не сохранил копию в папке «Отправленные»")
        except IMAPCollectorError:
            raise
        except (imaplib.IMAP4.error, ssl.SSLError, socket.timeout, OSError) as exc:
            raise IMAPCollectorError(
                "Не удалось сохранить копию письма в IMAP Mail.ru"
            ) from exc


def _safe_message_id(raw_message: bytes) -> str | None:
    try:
        return str(BytesParser(policy=policy.default).parsebytes(raw_message).get("Message-ID") or "")[:255] or None
    except Exception:
        return None


def _find_delivery(db: Session, bounce: DSNBounce, account_id: int) -> OutreachDelivery | None:
    if bounce.delivery_id:
        delivery = db.get(OutreachDelivery, bounce.delivery_id)
        if delivery and delivery.sender_account_id == account_id:
            return delivery
    if bounce.original_message_id:
        return db.scalar(
            select(OutreachDelivery).where(
                OutreachDelivery.sender_account_id == account_id,
                OutreachDelivery.message_id == bounce.original_message_id,
            )
        )
    if bounce.recipient:
        return db.scalar(
            select(OutreachDelivery)
            .where(
                OutreachDelivery.sender_account_id == account_id,
                OutreachDelivery.recipient == bounce.recipient,
                OutreachDelivery.status == "accepted",
            )
            .order_by(OutreachDelivery.accepted_at.desc(), OutreachDelivery.id.desc())
        )
    return None


def _add_suppression(
    db: Session,
    delivery: OutreachDelivery,
    bounce: DSNBounce,
    now: datetime,
) -> None:
    email = normalize_email(delivery.recipient)
    suppression = db.scalar(select(EmailSuppression).where(EmailSuppression.email == email))
    reason = "Подтверждён постоянный возврат почтового сервера"
    if suppression is None:
        suppression = EmailSuppression(email=email, reason=reason, source="imap_dsn")
        db.add(suppression)
    suppression.reason = reason
    suppression.source = "imap_dsn"
    suppression.campaign_id = delivery.campaign_id
    suppression.delivery_id = delivery.id
    suppression.smtp_code = bounce.status_code
    suppression.created_at = now
    suppression.lifted_at = None


def apply_dsn_bounce(
    db: Session,
    account: SenderAccount,
    uid: int,
    raw_message: bytes,
    *,
    now: datetime | None = None,
) -> bool:
    timestamp = now or datetime.now(timezone.utc)
    if db.scalar(
        select(ImapProcessedMessage).where(
            ImapProcessedMessage.sender_account_id == account.id,
            ImapProcessedMessage.uid == uid,
        )
    ):
        return False
    bounce = parse_permanent_dsn(raw_message)
    delivery = _find_delivery(db, bounce, account.id) if bounce else None
    outcome = "unrecognized"
    if bounce and delivery and delivery.status == "accepted":
        delivery.status = "bounced"
        delivery.error_message = "Поздний подтверждённый возврат получателя"
        delivery.smtp_code = bounce.status_code
        campaign = db.get(OutreachCampaign, delivery.campaign_id)
        if campaign:
            campaign.accepted_count = max(0, campaign.accepted_count - 1)
            campaign.sent_count = max(0, campaign.sent_count - 1)
            campaign.bounced_count += 1
        _add_suppression(db, delivery, bounce, timestamp)
        outcome = "bounced"
    db.add(
        ImapProcessedMessage(
            sender_account_id=account.id,
            uid=uid,
            message_id=_safe_message_id(raw_message),
            outcome=outcome,
            delivery_id=delivery.id if delivery else None,
        )
    )
    account.imap_last_uid = max(account.imap_last_uid, uid)
    account.updated_at = timestamp
    db.commit()
    logger.info(
        "imap_message_processed account_id=%s uid=%s outcome=%s delivery_id=%s",
        account.id,
        uid,
        outcome,
        delivery.id if delivery else None,
    )
    return outcome == "bounced"


def process_imap_tick(
    settings: Settings,
    *,
    session_factory: Callable[[], Session] | None = None,
    client_factory=MailruIMAPClient,
) -> None:
    cipher = CredentialCipher(settings.mail_credentials_encryption_key)
    with (session_factory or SessionLocal)() as db:
        accounts = list(
            db.scalars(
                select(SenderAccount).where(
                    SenderAccount.provider == "mailru_smtp",
                    SenderAccount.is_active.is_(True),
                    SenderAccount.imap_enabled.is_(True),
                    SenderAccount.encrypted_password.is_not(None),
                    SenderAccount.imap_verification_status != "failed",
                )
            ).all()
        )
        for account in accounts:
            original_password = account.encrypted_password
            try:
                password = cipher.decrypt(account.encrypted_password)
                with client_factory(
                    account,
                    password,
                    timeout_seconds=settings.mail_imap_timeout_seconds,
                ) as client:
                    messages = client.messages_after(
                        account.imap_last_uid,
                        settings.mail_imap_max_messages_per_tick,
                    )
                db.refresh(account, with_for_update=True)
                if account.encrypted_password != original_password or not account.is_active or not account.imap_enabled:
                    db.rollback()
                    continue
                account.imap_verification_status = "verified"
                account.imap_verification_error = None
                account.imap_verification_checked_at = datetime.now(timezone.utc)
                db.commit()
                for uid, raw_message in messages:
                    apply_dsn_bounce(db, account, uid, raw_message)
            except (CredentialEncryptionError, IMAPCollectorError) as exc:
                db.refresh(account, with_for_update=True)
                if account.encrypted_password != original_password or not account.is_active or not account.imap_enabled:
                    db.rollback()
                    continue
                category = exc.category if isinstance(exc, IMAPCollectorError) else "credentials"
                account.imap_verification_status = "temporary_error" if category in ("connection", "timeout", "temporary") else "failed"
                account.imap_verification_error = str(exc)
                account.imap_verification_checked_at = datetime.now(timezone.utc)
                db.commit()
                logger.warning("imap_account_check_failed account_id=%s category=%s", account.id, category)


async def run_imap_worker(settings: Settings) -> None:
    while True:
        try:
            from app.services.sender_accounts import recover_temporary_sender_accounts
            await asyncio.to_thread(recover_temporary_sender_accounts, settings)
            await asyncio.to_thread(process_imap_tick, settings)
        except CredentialEncryptionError:
            pass
        except Exception:
            logger.exception("imap_worker_tick_failed")
        await asyncio.sleep(float(settings.mail_imap_worker_poll_seconds))
