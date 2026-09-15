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

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.mail_providers import SMTP_SENDER_PROVIDERS
from app.config import Settings
from app.database import SessionLocal
from app.models import (
    EmailSuppression,
    ImapProcessedMessage,
    ImapReplyFolder,
    OutreachCampaign,
    OutreachDelivery,
    SenderAccount,
)
from app.services.credentials import CredentialCipher, CredentialEncryptionError
from app.services.dsn import DSNBounce, parse_permanent_dsn
from app.services.provider import normalize_email
from app.services.inbound_replies import apply_client_reply


logger = logging.getLogger("fuellead.imap")

IMAP_LIST_RE = re.compile(
    rb"^\((?P<flags>[^)]*)\)\s+(?:NIL|\"(?:\\.|[^\"])*\")\s+(?P<mailbox>.+)$",
    re.IGNORECASE,
)


class IMAPCollectorError(RuntimeError):
    def __init__(self, message: str, *, category: str = "protocol"):
        super().__init__(message)
        self.category = category


def imap_failure_status(category: str) -> str:
    # Only unusable credentials require user action. A certificate, protocol,
    # or folder failure can recover without replacing an application password.
    return "failed" if category in ("auth", "credentials") else "temporary_error"


def _imap_revision(account: SenderAccount) -> tuple:
    checked_at = account.imap_verification_checked_at
    return (
        account.imap_verification_status,
        account.imap_verification_error,
        checked_at.replace(tzinfo=None) if checked_at else None,
        account.imap_uidvalidity,
    )


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
        self.uidvalidity: int | None = None
        self.uid_namespace_reset = False
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
                category, message = "timeout", "IMAP не ответил вовремя; SMTP проверяется отдельно"
            elif isinstance(exc, (ssl.SSLEOFError, ssl.SSLZeroReturnError)):
                category, message = "connection", "Соединение TLS с IMAP прервано; проверка повторится позже"
            elif isinstance(exc, ssl.SSLError):
                category, message = "tls", "Ошибка TLS-соединения с IMAP; проверьте сеть, сертификаты и время системы"
            elif isinstance(exc, (imaplib.IMAP4.abort, OSError)):
                category, message = "connection", "Соединение с IMAP прервано; SMTP проверяется отдельно"
            elif isinstance(exc, imaplib.IMAP4.error) and stage == "login":
                if any(marker in str(exc).upper() for marker in ("UNAVAILABLE", "LIMIT", "TRY AGAIN", "TOO MANY", "SERVERBUG", "TEMPORAR")):
                    category, message = "temporary", "Почтовый сервер временно ограничил IMAP-подключения; проверка повторится позже"
                else:
                    category, message = "auth", "IMAP отклонил вход. Проверьте доступ по IMAP и пароль приложения в почтовом сервисе"
            else:
                category, message = "protocol", "Не удалось выполнить IMAP-операцию Почтовый сервер"
            raise IMAPCollectorError(message, category=category) from exc

    def __exit__(self, *_):
        if self.client is None:
            return
        with suppress(Exception):
            self.client.close()
        with suppress(Exception):
            self.client.logout()
        # logout can itself fail after a server disconnect. Ensure the local
        # socket is closed even in that case.
        with suppress(Exception):
            self.client.shutdown()
        self.client = None

    def messages_after(self, last_uid: int, limit: int, *, mailbox="INBOX", expected_uidvalidity=None) -> list[tuple[int, bytes]]:
        try:
            return self._messages_after(last_uid, limit, mailbox=mailbox, expected_uidvalidity=expected_uidvalidity)
        except IMAPCollectorError:
            raise
        except (imaplib.IMAP4.error, OSError) as exc:
            raise IMAPCollectorError("Не удалось получить новые сообщения IMAP; проверка повторится позже", category="connection") from exc

    def _messages_after(self, last_uid: int, limit: int, *, mailbox="INBOX", expected_uidvalidity=None) -> list[tuple[int, bytes]]:
        assert self.client is not None
        # APPEND and login-only checks do not need INBOX to be available.
        status, _ = self.client.select(mailbox, readonly=True)
        if status != "OK":
            raise IMAPCollectorError("Почтовый сервер временно не открыл папку входящих сообщений", category="temporary")
        self.uidvalidity = self._mailbox_counter("UIDVALIDITY", required=True)
        uidnext = self._mailbox_counter("UIDNEXT", required=False)
        previous_validity = expected_uidvalidity if expected_uidvalidity is not None else (self.account.imap_uidvalidity if mailbox == "INBOX" else None)
        self.uid_namespace_reset = previous_validity not in (None, self.uidvalidity) or (
            previous_validity is None and uidnext is not None and uidnext <= last_uid
        )
        if self.uid_namespace_reset:
            last_uid = 0
        status, values = self.client.uid("search", None, f"UID {last_uid + 1}:*")
        if status != "OK":
            raise IMAPCollectorError("Почтовый сервер не вернул список новых сообщений", category="temporary")
        raw_uids = values[0].split() if values and values[0] else []
        # IMAP ranges are inclusive in both directions: N:* may return the
        # last existing UID even when it is below N.
        try:
            uids = sorted({int(uid) for uid in raw_uids if int(uid) > last_uid})
        except (TypeError, ValueError) as exc:
            raise IMAPCollectorError("Почтовый сервер вернул некорректный список сообщений", category="temporary") from exc
        result: list[tuple[int, bytes]] = []
        for uid in uids[:limit]:
            raw_uid = str(uid).encode("ascii")
            status, payload = self.client.uid("fetch", raw_uid, "(BODY.PEEK[])")
            if status != "OK":
                # Advancing past this UID would lose its bounce forever.
                raise IMAPCollectorError("Почтовый сервер временно не вернул сообщение; чтение повторится позже", category="temporary")
            raw_message = next(
                (item[1] for item in payload or [] if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes)),
                None,
            )
            if raw_message is not None:
                result.append((uid, raw_message))
            elif any(item for item in payload or []):
                raise IMAPCollectorError("Почтовый сервер вернул неполное сообщение; чтение повторится позже", category="temporary")
            # An empty successful FETCH means the message was expunged since
            # SEARCH. It has no remaining contents to collect.
        return result

    def reply_mailboxes(self) -> list[str]:
        status, values = self.client.list()
        if status != "OK":
            raise IMAPCollectorError("Не удалось получить папки ответов IMAP", category="temporary")
        result = []
        for value in values or []:
            match = IMAP_LIST_RE.match(value) if isinstance(value, bytes) else None
            if not match:
                continue
            flags = match.group("flags").lower().split()
            mailbox = match.group("mailbox").decode("ascii")
            if mailbox.strip('"').upper() == "INBOX" or any(flag in flags for flag in (b"\\sent", b"\\drafts", b"\\noselect")):
                continue
            result.append(mailbox)
        return result

    def _mailbox_counter(self, name: str, *, required: bool) -> int | None:
        assert self.client is not None
        _, values = self.client.response(name)
        value = values[0] if values else None
        if value is None and not required:
            return None
        try:
            parsed = int(value)
            if not 0 < parsed <= 4294967295:
                raise ValueError()
            return parsed
        except (TypeError, ValueError) as exc:
            raise IMAPCollectorError("Почтовый сервер не сообщил корректные данные папки IMAP; чтение повторится позже", category="temporary") from exc

    def append_sent(self, raw_message: bytes, sent_at: datetime) -> None:
        """Save an accepted SMTP message in Почтовый сервер's server-side Sent folder."""
        assert self.client is not None
        try:
            status, values = self.client.list()
            if status != "OK":
                raise IMAPCollectorError("Почтовый сервер не вернул список почтовых папок")

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
                raise IMAPCollectorError("Почтовый сервер не сообщил системную папку «Отправленные»")

            status, _ = self.client.append(sent_mailbox, r"(\Seen)", sent_at, raw_message)
            if status != "OK":
                raise IMAPCollectorError("Почтовый сервер не сохранил копию в папке «Отправленные»")
        except IMAPCollectorError:
            raise
        except (imaplib.IMAP4.error, ssl.SSLError, socket.timeout, OSError) as exc:
            raise IMAPCollectorError(
                "Не удалось сохранить копию письма в IMAP"
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
    expected_uidvalidity: int | None = None,
    reprocess: bool = False,
) -> bool:
    timestamp = now or datetime.now(timezone.utc)
    # Serialize UID tracking for this mailbox and reject a batch fetched before
    # another collector observed a rebuilt INBOX.
    db.refresh(account, with_for_update=True)
    if expected_uidvalidity is not None and account.imap_uidvalidity != expected_uidvalidity:
        db.rollback()
        return False
    processed = db.scalar(
        select(ImapProcessedMessage).where(
            ImapProcessedMessage.sender_account_id == account.id,
            ImapProcessedMessage.uid == uid,
        )
    )
    if processed and (not reprocess or processed.outcome not in ("unrecognized", "unmatched_reply")):
        db.rollback()
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
    reply_delivery_id = None
    if not bounce:
        outcome, reply_delivery_id = apply_client_reply(db, account.id, raw_message, now=timestamp, uid=uid)
    if processed:
        processed.outcome = outcome
        processed.delivery_id = delivery.id if delivery else reply_delivery_id
    else:
        db.add(ImapProcessedMessage(
            sender_account_id=account.id,
            uid=uid,
            message_id=_safe_message_id(raw_message),
            outcome=outcome,
            delivery_id=delivery.id if delivery else reply_delivery_id,
        ))
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
        account_ids = list(
            db.scalars(
                select(SenderAccount.id).where(
                    SenderAccount.provider.in_(SMTP_SENDER_PROVIDERS),
                    SenderAccount.is_active.is_(True),
                    SenderAccount.imap_enabled.is_(True),
                    SenderAccount.encrypted_password.is_not(None),
                    SenderAccount.imap_verification_status != "failed",
                )
            ).all()
        )
        for account_id in account_ids:
            try:
                account = db.get(SenderAccount, account_id)
                if account is None or not account.is_active or not account.imap_enabled:
                    continue
                original_password = account.encrypted_password
                original_revision = _imap_revision(account)
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
                account = db.get(SenderAccount, account_id, populate_existing=True, with_for_update=True)
                if account is None or account.encrypted_password != original_password or _imap_revision(account) != original_revision or not account.is_active or not account.imap_enabled:
                    db.rollback()
                    continue
                uidvalidity = getattr(client, "uidvalidity", None)
                if getattr(client, "uid_namespace_reset", False):
                    # Only ephemeral UID tracking belongs to the old namespace.
                    # Confirmed bounces, deliveries and suppressions are history.
                    db.execute(delete(ImapProcessedMessage).where(ImapProcessedMessage.sender_account_id == account.id))
                    account.imap_last_uid = 0
                    logger.info("imap_uid_namespace_changed account_id=%s", account.id)
                if uidvalidity is not None:
                    account.imap_uidvalidity = uidvalidity
                account.imap_verification_status = "verified"
                account.imap_verification_error = None
                account.imap_verification_checked_at = datetime.now(timezone.utc)
                db.commit()
                for uid, raw_message in messages:
                    apply_dsn_bounce(db, account, uid, raw_message, expected_uidvalidity=uidvalidity)
            except (CredentialEncryptionError, IMAPCollectorError) as exc:
                account = db.get(SenderAccount, account_id, populate_existing=True, with_for_update=True)
                if account is None or account.encrypted_password != original_password or _imap_revision(account) != original_revision or not account.is_active or not account.imap_enabled:
                    db.rollback()
                    continue
                category = exc.category if isinstance(exc, IMAPCollectorError) else "credentials"
                account.imap_verification_status = imap_failure_status(category)
                account.imap_verification_error = str(exc)
                account.imap_verification_checked_at = datetime.now(timezone.utc)
                db.commit()
                logger.warning("imap_account_check_failed account_id=%s category=%s", account.id, category)
            except Exception as exc:
                # A malformed message or an account deleted during the check
                # must not prevent other mailboxes from being processed.
                db.rollback()
                logger.error("imap_account_processing_failed account_id=%s error_type=%s", account_id, type(exc).__name__)


def process_reply_folders_tick(settings: Settings, *, session_factory=None, client_factory=MailruIMAPClient) -> None:
    """Folder-scoped cursors; moving a reply cannot hide it behind INBOX's UID."""
    cipher = CredentialCipher(settings.mail_credentials_encryption_key)
    with (session_factory or SessionLocal)() as db:
        ids = list(db.scalars(select(SenderAccount.id).where(
            SenderAccount.is_active.is_(True), SenderAccount.imap_enabled.is_(True),
            SenderAccount.encrypted_password.is_not(None), SenderAccount.imap_verification_status != "failed",
            SenderAccount.provider.in_(SMTP_SENDER_PROVIDERS),
        )))
        for account_id in ids:
            account = db.get(SenderAccount, account_id, populate_existing=True)
            original_password = account.encrypted_password
            revision = _imap_revision(account)
            try:
                with client_factory(account, cipher.decrypt(original_password), timeout_seconds=settings.mail_imap_timeout_seconds) as client:
                    for mailbox in client.reply_mailboxes():
                        state = db.scalar(select(ImapReplyFolder).where(
                            ImapReplyFolder.sender_account_id == account_id, ImapReplyFolder.mailbox == mailbox))
                        previous_uid = state.last_uid if state else 0
                        previous_validity = state.uidvalidity if state else None
                        messages = client.messages_after(previous_uid, settings.mail_imap_max_messages_per_tick,
                                                         mailbox=mailbox, expected_uidvalidity=previous_validity)
                        db.refresh(account, with_for_update=True)
                        if account.encrypted_password != original_password or _imap_revision(account) != revision or not account.is_active or not account.imap_enabled:
                            db.rollback()
                            break
                        state = db.scalar(select(ImapReplyFolder).where(
                            ImapReplyFolder.sender_account_id == account_id, ImapReplyFolder.mailbox == mailbox)
                                          .execution_options(populate_existing=True).with_for_update())
                        if state and (state.last_uid != previous_uid or state.uidvalidity != previous_validity):
                            db.rollback()
                            continue
                        if state is None:
                            state = ImapReplyFolder(sender_account_id=account_id, mailbox=mailbox, last_uid=0)
                            db.add(state)
                        if client.uid_namespace_reset:
                            state.last_uid = 0
                        state.uidvalidity = client.uidvalidity
                        for uid, raw in messages:
                            outcome, _ = apply_client_reply(db, account_id, raw, mailbox=mailbox, uid=uid)
                            state.last_uid = max(state.last_uid, uid)
                            if outcome in ("rejected", "answered"):
                                logger.info("imap_folder_reply account_id=%s uid=%s outcome=%s", account_id, uid, outcome)
                        db.commit()
            except Exception as exc:
                db.rollback()
                account = db.get(SenderAccount, account_id, populate_existing=True, with_for_update=True)
                if isinstance(exc, (IMAPCollectorError, CredentialEncryptionError)) and account and account.encrypted_password == original_password and _imap_revision(account) == revision:
                    category = exc.category if isinstance(exc, IMAPCollectorError) else "credentials"
                    account.imap_verification_status = imap_failure_status(category)
                    account.imap_verification_error = str(exc)
                    account.imap_verification_checked_at = datetime.now(timezone.utc)
                    db.commit()
                logger.error("imap_reply_folders_failed account_id=%s error_type=%s", account_id, type(exc).__name__)


async def run_imap_worker(settings: Settings) -> None:
    from app.services.sender_accounts import recover_temporary_sender_accounts

    while True:
        for stage, operation in (("imap_collection", process_imap_tick), ("imap_reply_folders", process_reply_folders_tick), ("smtp_recovery", recover_temporary_sender_accounts)):
            try:
                await asyncio.to_thread(operation, settings)
            except Exception as exc:
                logger.error("mail_worker_stage_failed stage=%s error_type=%s", stage, type(exc).__name__)
        await asyncio.sleep(float(settings.mail_imap_worker_poll_seconds))
