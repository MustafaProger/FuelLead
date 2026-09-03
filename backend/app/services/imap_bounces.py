import asyncio
import imaplib
import logging
import re
import socket
import ssl
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
    pass


class MailruIMAPClient:
    def __init__(
        self,
        account: SenderAccount,
        password: str,
        *,
        timeout_seconds: float,
        imap_factory=imaplib.IMAP4_SSL,
    ):
        self.account = account
        self.password = password
        self.timeout_seconds = timeout_seconds
        self.imap_factory = imap_factory
        self.client = None

    def __enter__(self):
        try:
            context = ssl.create_default_context()
            self.client = self.imap_factory(
                self.account.imap_host,
                self.account.imap_port,
                ssl_context=context,
                timeout=self.timeout_seconds,
            )
            self.client.login(self.account.email, self.password)
            status, _ = self.client.select("INBOX", readonly=True)
            if status != "OK":
                raise IMAPCollectorError("Mail.ru не открыл папку входящих сообщений")
            return self
        except (imaplib.IMAP4.error, ssl.SSLError, socket.timeout, OSError) as exc:
            raise IMAPCollectorError(
                "Не удалось безопасно подключиться к IMAP Mail.ru"
            ) from exc

    def __exit__(self, *_):
        if self.client is None:
            return
        with suppress(Exception):
            self.client.close()
        with suppress(Exception):
            self.client.logout()

    def messages_after(self, last_uid: int, limit: int) -> list[tuple[int, bytes]]:
        assert self.client is not None
        status, values = self.client.uid("search", None, f"UID {last_uid + 1}:*")
        if status != "OK":
            raise IMAPCollectorError("Mail.ru не вернул список новых сообщений")
        raw_uids = values[0].split() if values and values[0] else []
        result: list[tuple[int, bytes]] = []
        for raw_uid in raw_uids[:limit]:
            uid = int(raw_uid)
            status, payload = self.client.uid("fetch", raw_uid, "(RFC822)")
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
                )
            ).all()
        )
        for account in accounts:
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
                for uid, raw_message in messages:
                    apply_dsn_bounce(db, account, uid, raw_message)
            except (CredentialEncryptionError, IMAPCollectorError):
                logger.warning("imap_account_check_failed account_id=%s", account.id)


async def run_imap_worker(settings: Settings) -> None:
    while True:
        try:
            await asyncio.to_thread(process_imap_tick, settings)
        except CredentialEncryptionError:
            pass
        except Exception:
            logger.exception("imap_worker_tick_failed")
        await asyncio.sleep(float(settings.mail_imap_worker_poll_seconds))
