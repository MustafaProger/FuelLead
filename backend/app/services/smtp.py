import logging
import re
import smtplib
import socket
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from datetime import datetime, timezone

from app.models import SenderAccount
from app.services.imap_bounces import IMAPCollectorError, MailruIMAPClient
from app.services.provider import normalize_email


ENHANCED_STATUS_RE = re.compile(r"\b([245]\.\d{1,3}\.\d{1,3})\b")
SECRET_PATTERNS = (
    re.compile(r"(?i)\bAUTH\s+[^\r\n]*"),
    re.compile(r"(?i)\b(password|passwd|token)\s*[=:]\s*\S+"),
)

logger = logging.getLogger("fuellead.smtp")


def safe_smtp_text(value: bytes | str | None, *, secret: str = "") -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value or "")
    text = " ".join(text.replace("\x00", " ").split())
    if secret:
        text = text.replace(secret, "[REDACTED]")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[:500]


def smtp_status_code(code: int | None, response: bytes | str | None) -> str | None:
    match = ENHANCED_STATUS_RE.search(safe_smtp_text(response))
    if match:
        return match.group(1)
    return str(code) if code is not None else None


class SMTPDeliveryError(RuntimeError):
    def __init__(
        self,
        safe_message: str,
        *,
        category: str,
        smtp_code: str | None = None,
        permanent_recipient_failure: bool = False,
        uncertain: bool = False,
    ):
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.category = category
        self.smtp_code = smtp_code
        self.permanent_recipient_failure = permanent_recipient_failure
        self.uncertain = uncertain


@dataclass(frozen=True, slots=True)
class SMTPAccepted:
    message_id: str
    smtp_code: str
    smtp_response: str
    sent_copy_saved: bool | None = None
    sent_copy_error: str | None = None


def _map_connect_error(exc: BaseException, *, password: str = "") -> SMTPDeliveryError:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        code = smtp_status_code(getattr(exc, "smtp_code", None), getattr(exc, "smtp_error", None))
        return SMTPDeliveryError(
            "Mail.ru отклонил авторизацию. Проверьте адрес и новый пароль внешнего приложения",
            category="auth",
            smtp_code=code,
        )
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return SMTPDeliveryError(
            "Mail.ru не ответил вовремя. Повторите проверку позже",
            category="timeout",
        )
    if isinstance(exc, (ssl.SSLError, ssl.CertificateError)):
        return SMTPDeliveryError(
            "Не удалось установить защищённое TLS-соединение с Mail.ru",
            category="tls",
        )
    if isinstance(exc, smtplib.SMTPResponseException):
        code_value = int(getattr(exc, "smtp_code", 0) or 0)
        code = smtp_status_code(code_value, getattr(exc, "smtp_error", None))
        if code_value in (421, 450, 451, 452) or 400 <= code_value < 500:
            message = "Mail.ru сообщил о временной ошибке или ограничении ящика"
            category = "temporary"
        elif code_value in (530, 534, 535, 538):
            message = "Mail.ru отклонил авторизацию ящика"
            category = "auth"
        else:
            message = "Mail.ru отклонил SMTP-операцию"
            category = "provider"
        return SMTPDeliveryError(message, category=category, smtp_code=code)
    return SMTPDeliveryError(
        "Не удалось подключиться к Mail.ru",
        category="connection",
    )


class MailruSMTPClient:
    """Synchronous implicit-TLS SMTP client. Tests inject an isolated factory."""

    def __init__(
        self,
        account: SenderAccount,
        password: str,
        *,
        timeout_seconds: float = 30.0,
        smtp_factory=smtplib.SMTP_SSL,
        imap_timeout_seconds: float | None = None,
        imap_client_factory=MailruIMAPClient,
        connect_attempts: int = 3,
        connect_retry_delay_seconds: float = 1.0,
        sleep_func=time.sleep,
    ):
        self.account = account
        self.password = password
        self.timeout_seconds = timeout_seconds
        self.smtp_factory = smtp_factory
        self.imap_timeout_seconds = imap_timeout_seconds or timeout_seconds
        self.imap_client_factory = imap_client_factory
        self.connect_attempts = max(1, connect_attempts)
        self.connect_retry_delay_seconds = max(0.0, connect_retry_delay_seconds)
        self.sleep_func = sleep_func

    def _connect(self):
        context = ssl.create_default_context()
        last_error = None
        for attempt in range(1, self.connect_attempts + 1):
            smtp = None
            try:
                smtp = self.smtp_factory(
                    self.account.smtp_host or "smtp.mail.ru",
                    self.account.smtp_port or 465,
                    timeout=self.timeout_seconds,
                    context=context,
                )
                smtp.ehlo()
                smtp.login(self.account.email, self.password)
                return smtp
            except Exception as exc:
                if smtp is not None:
                    try:
                        smtp.close()
                    except Exception:
                        pass
                last_error = _map_connect_error(exc, password=self.password)
                retryable = last_error.category in ("connection", "timeout", "temporary")
                if not retryable or attempt >= self.connect_attempts:
                    raise last_error from exc
                logger.warning(
                    "smtp_connect_retry account_id=%s attempt=%s max_attempts=%s category=%s",
                    self.account.id,
                    attempt,
                    self.connect_attempts,
                    last_error.category,
                )
                self.sleep_func(self.connect_retry_delay_seconds * attempt)
        assert last_error is not None
        raise last_error

    def _save_sent_copy(self, raw_message: bytes, sent_at: datetime) -> tuple[bool, str | None]:
        if not self.account.imap_enabled:
            return False, "IMAP отключён для этого ящика"
        try:
            with self.imap_client_factory(
                self.account,
                self.password,
                timeout_seconds=self.imap_timeout_seconds,
            ) as imap:
                imap.append_sent(raw_message, sent_at)
            return True, None
        except IMAPCollectorError as exc:
            error = str(exc)
        except Exception:
            error = "Не удалось сохранить копию письма в IMAP Mail.ru"
        logger.warning(
            "imap_sent_copy_failed account_id=%s message_id_present=true reason=%s",
            self.account.id,
            error,
        )
        return False, error

    def verify(self) -> None:
        smtp = self._connect()
        try:
            smtp.quit()
        except (smtplib.SMTPException, OSError):
            smtp.close()

    def send(
        self,
        recipient: str,
        subject: str,
        text_body: str,
        *,
        delivery_id: int | None = None,
        campaign_id: int | None = None,
    ) -> SMTPAccepted:
        sender = normalize_email(self.account.email)
        target = normalize_email(recipient)
        if not sender or not target:
            raise ValueError("Адрес отправителя или получателя некорректен")
        if not subject.strip() or "\n" in subject or "\r" in subject:
            raise ValueError("Тема письма должна занимать одну строку")
        if not text_body.strip():
            raise ValueError("Текст письма обязателен")

        sent_at = datetime.now(timezone.utc)
        message = EmailMessage()
        message["To"] = target
        message["From"] = formataddr(((self.account.display_name or "").strip(), sender))
        message["Reply-To"] = sender
        message["Subject"] = subject.strip()
        message["Date"] = format_datetime(sent_at)
        message_id = make_msgid(domain=sender.rsplit("@", 1)[1])
        message["Message-ID"] = message_id
        message["List-Unsubscribe"] = f"<mailto:{sender}?subject=unsubscribe>"
        if delivery_id is not None:
            message["X-FuelLead-Delivery-ID"] = str(delivery_id)
        if campaign_id is not None:
            message["X-FuelLead-Campaign-ID"] = str(campaign_id)
        message.set_content(text_body)

        raw_message = message.as_bytes()
        smtp = self._connect()
        data_started = False
        accepted = None
        try:
            code, response = smtp.mail(sender)
            if not 200 <= code < 300:
                raise smtplib.SMTPSenderRefused(code, response, sender)
            code, response = smtp.rcpt(target)
            if not 200 <= code < 300:
                technical_code = smtp_status_code(code, response)
                raise SMTPDeliveryError(
                    "Адрес получателя подтверждённо отклонён почтовым сервером",
                    category="recipient",
                    smtp_code=technical_code,
                    permanent_recipient_failure=500 <= code < 600,
                )
            data_started = True
            code, response = smtp.data(raw_message)
            if not 200 <= code < 300:
                technical_code = smtp_status_code(code, response)
                if 500 <= code < 600:
                    raise SMTPDeliveryError(
                        "Почтовый сервер окончательно отклонил письмо",
                        category="provider",
                        smtp_code=technical_code,
                    )
                raise SMTPDeliveryError(
                    "Почтовый сервер временно не принял письмо",
                    category="temporary",
                    smtp_code=technical_code,
                )
            accepted = SMTPAccepted(
                message_id=message_id,
                smtp_code=smtp_status_code(code, response) or str(code),
                smtp_response=safe_smtp_text(response, secret=self.password),
            )
        except SMTPDeliveryError:
            raise
        except (smtplib.SMTPServerDisconnected, socket.timeout, TimeoutError) as exc:
            if data_started:
                raise SMTPDeliveryError(
                    "Соединение оборвалось после начала SMTP-попытки; результат неизвестен",
                    category="uncertain",
                    uncertain=True,
                ) from exc
            raise _map_connect_error(exc, password=self.password) from exc
        except Exception as exc:
            mapped = _map_connect_error(exc, password=self.password)
            if data_started and mapped.category in ("connection", "timeout"):
                mapped = SMTPDeliveryError(
                    "Соединение оборвалось после начала SMTP-попытки; результат неизвестен",
                    category="uncertain",
                    uncertain=True,
                )
            raise mapped from exc
        finally:
            try:
                smtp.quit()
            except Exception:
                try:
                    smtp.close()
                except Exception:
                    pass
        assert accepted is not None
        sent_copy_saved, sent_copy_error = self._save_sent_copy(raw_message, sent_at)
        return SMTPAccepted(
            message_id=accepted.message_id,
            smtp_code=accepted.smtp_code,
            smtp_response=accepted.smtp_response,
            sent_copy_saved=sent_copy_saved,
            sent_copy_error=sent_copy_error,
        )
