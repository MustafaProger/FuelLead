import logging
import base64
import binascii
import re
import smtplib
import socket
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email import policy
from email.utils import format_datetime, formataddr, make_msgid
from datetime import datetime, timezone

from app.mail_providers import MAIL_PROVIDERS
from app.models import SenderAccount
from app.services.imap_bounces import IMAPCollectorError, MailruIMAPClient
from app.services.provider import normalize_email


ENHANCED_STATUS_RE = re.compile(r"\b([245]\.\d{1,3}\.\d{1,3})\b")
# RFC 3463: 5.1.7/5.1.8 concern the sender, while 5.2.2 (full)
# and 5.2.3 (message too large) do not make a recipient nonexistent.
PERMANENT_RECIPIENT_CODES = frozenset(("5.1.1", "5.1.2", "5.1.3", "5.1.6", "5.2.1"))
BASE64_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{8,}={0,2}(?![A-Za-z0-9+/=])")
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
        # LOGIN and PLAIN payloads must never survive provider error reporting.
        text = text.replace(base64.b64encode(secret.encode()).decode(), "[REDACTED]")
        def redact_auth_payload(match: re.Match) -> str:
            token = match.group(0)
            try:
                decoded = base64.b64decode(token + "=" * (-len(token) % 4), validate=True)
            except (ValueError, binascii.Error):
                return token
            return "[REDACTED]" if secret.encode() in decoded else token
        text = BASE64_TOKEN_RE.sub(redact_auth_payload, text)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[:500]


def smtp_status_code(code: int | None, response: bytes | str | None) -> str | None:
    match = ENHANCED_STATUS_RE.search(safe_smtp_text(response))
    if match:
        return match.group(1)
    return str(code) if code is not None else None


def _smtp_address(value: str) -> str | None:
    normalized = normalize_email(value)
    if not normalized:
        return None
    local, domain = normalized.rsplit("@", 1)
    try:
        # Domains can be encoded as IDNA; non-ASCII local parts require the
        # SMTPUTF8 extension, which this transport does not negotiate.
        local.encode("ascii")
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return f"{local}@{domain}"


class SMTPDeliveryError(RuntimeError):
    def __init__(
        self,
        safe_message: str,
        *,
        category: str,
        smtp_code: str | None = None,
        permanent_recipient_failure: bool = False,
        uncertain: bool = False,
        smtp_response: str | None = None,
    ):
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.category = category
        self.smtp_code = smtp_code
        self.permanent_recipient_failure = permanent_recipient_failure
        self.uncertain = uncertain
        self.smtp_response = smtp_response


@dataclass(frozen=True, slots=True)
class SMTPAccepted:
    message_id: str
    smtp_code: str
    smtp_response: str
    sent_copy_saved: bool | None = None
    sent_copy_error: str | None = None


def _map_connect_error(exc: BaseException, *, password: str = "") -> SMTPDeliveryError:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        numeric_code = int(exc.smtp_code)
        code = smtp_status_code(numeric_code, exc.smtp_error)
        response = safe_smtp_text(exc.smtp_error, secret=password)
        diagnostic = f"SMTP {numeric_code}" + (f" / {code}" if code != str(numeric_code) else "")
        if 400 <= numeric_code < 500:
            return SMTPDeliveryError(
                f"Почтовый сервер временно отложил авторизацию ({diagnostic}). Пароль менять не требуется; проверка повторится позже" + (f": {response}" if response else ""),
                category="temporary", smtp_code=code, smtp_response=response,
            )
        return SMTPDeliveryError(
            f"Почтовый сервер отклонил авторизацию ({diagnostic}). Проверьте доступ по IMAP/SMTP, адрес и пароль внешнего приложения в настройках почтового сервиса" + (f": {response}" if response else ""),
            category="auth",
            smtp_code=code,
            smtp_response=response,
        )
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return SMTPDeliveryError(
            "Почтовый сервер не ответил вовремя. Повторите проверку позже",
            category="timeout",
        )
    if isinstance(exc, (ssl.SSLEOFError, ssl.SSLZeroReturnError)):
        return SMTPDeliveryError("Соединение TLS с почтовым сервером прервано до передачи письма; проверка повторится позже", category="connection")
    if isinstance(exc, (ssl.SSLError, ssl.CertificateError)):
        return SMTPDeliveryError(
            "Не удалось установить защищённое TLS-соединение с почтовым сервером",
            category="tls",
        )
    if isinstance(exc, socket.gaierror):
        return SMTPDeliveryError("Не удалось определить адрес почтового сервера (DNS). Проверьте сеть и VPN", category="connection")
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return SMTPDeliveryError("Почтовый сервер закрыл SMTP-соединение до передачи письма. Проверка повторится позже", category="connection")
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return SMTPDeliveryError(
            "SMTP-сервер не поддерживает авторизацию. Проверьте адрес SMTP-сервера и порт 465 с TLS",
            category="provider",
        )
    if isinstance(exc, smtplib.SMTPResponseException):
        code_value = int(getattr(exc, "smtp_code", 0) or 0)
        code = smtp_status_code(code_value, getattr(exc, "smtp_error", None))
        if code_value in (421, 450, 451, 452) or 400 <= code_value < 500:
            message = "Почтовый сервер сообщил о временной ошибке или ограничении ящика"
            category = "temporary"
        elif code_value in (530, 534, 535, 538):
            message = "Почтовый сервер отклонил авторизацию ящика"
            category = "auth"
        else:
            message = "Почтовый сервер отклонил SMTP-операцию"
            category = "provider"
        response = safe_smtp_text(getattr(exc, "smtp_error", None), secret=password)
        return SMTPDeliveryError(f"{message} (SMTP {code})" + (f": {response}" if response else ""), category=category, smtp_code=code, smtp_response=response)
    return SMTPDeliveryError(
        "Не удалось подключиться к почтовому серверу",
        category="connection",
    )


def _rejection(code: int, response: bytes, *, stage: str, password: str, recipient: str | None = None) -> SMTPDeliveryError:
    mapped = _map_connect_error(smtplib.SMTPResponseException(code, response), password=password)
    enhanced = smtp_status_code(code, response) or ""
    recipient_stage = stage in ("RCPT", "DATA")
    # Mail.ru sometimes validates its local recipient only after DATA and
    # returns plain 550 instead of an enhanced status. Match the actual sole
    # recipient: a sender failure or an unrelated address must not suppress it.
    mailru_missing_recipient = bool(
        recipient_stage
        and recipient
        and re.search(
            rf"\blocal mailbox\s+<?{re.escape(recipient)}>?\s+is unavailable:\s*user (?:not found|is terminated)\b",
            safe_smtp_text(response),
            re.IGNORECASE,
        )
    )
    permanent_recipient = (
        recipient_stage and 500 <= code < 600
        and (enhanced in PERMANENT_RECIPIENT_CODES or mailru_missing_recipient)
    )
    if permanent_recipient:
        mapped.category = "recipient"
        mapped.permanent_recipient_failure = True
    elif (
        enhanced in ("4.2.3", "5.2.3", "4.3.4", "5.3.4")
        or enhanced.startswith(("4.6.", "5.6."))
    ):
        mapped.category = "content"
    elif recipient_stage and (
        enhanced.startswith(("4.2.", "5.2."))
        or enhanced in ("4.1.1", "4.1.2", "4.1.3", "4.1.4", "4.1.6", "5.1.4")
    ):
        mapped.category = "recipient"
    mapped.safe_message = f"{stage}: {mapped.safe_message}"
    mapped.args = (mapped.safe_message,)
    return mapped


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
                    self.account.smtp_host or MAIL_PROVIDERS[self.account.provider or "mailru_smtp"].smtp_host,
                    self.account.smtp_port or 465,
                    timeout=self.timeout_seconds,
                    context=context,
                )
                code, response = smtp.ehlo()
                if not 200 <= code < 300:
                    raise smtplib.SMTPHeloError(code, response)
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
        preset = MAIL_PROVIDERS.get(self.account.provider)
        if preset and preset.automatic_sent_copy:
            # Gmail stores SMTP submissions in Sent itself; APPEND duplicates them.
            return True, None
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
            error = "Не удалось сохранить копию письма в IMAP"
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
            try:
                smtp.close()
            except Exception:
                pass

    def send(
        self,
        recipient: str,
        subject: str,
        text_body: str,
        *,
        delivery_id: int | None = None,
        campaign_id: int | None = None,
        in_reply_to: str | None = None,
        references: tuple[str, ...] = (),
        message_id: str | None = None,
    ) -> SMTPAccepted:
        sender = _smtp_address(self.account.email)
        target = _smtp_address(recipient)
        if not sender:
            raise SMTPDeliveryError("Адрес отправителя некорректен", category="provider")
        if not target:
            raise SMTPDeliveryError(
                "Адрес получателя некорректен или содержит нелатинские символы до @",
                category="recipient",
            )
        if not subject.strip() or "\n" in subject or "\r" in subject:
            raise SMTPDeliveryError("Тема письма должна занимать одну непустую строку", category="content")
        if not text_body.strip():
            raise SMTPDeliveryError("Текст письма обязателен", category="content")
        for header in (in_reply_to, message_id, *references):
            if header is not None and not re.fullmatch(r"<[^<>\s\x00-\x1f\x7f]{1,250}>", header):
                raise SMTPDeliveryError("Некорректный идентификатор письма", category="content")
        display_name = (self.account.display_name or "").strip()
        if "\r" in display_name or "\n" in display_name:
            raise SMTPDeliveryError("Имя отправителя должно занимать одну строку", category="provider")

        sent_at = datetime.now(timezone.utc)
        message = EmailMessage(policy=policy.SMTP)
        message["To"] = target
        message["From"] = formataddr((display_name, sender))
        message["Reply-To"] = sender
        message["Subject"] = subject.strip()
        message["Date"] = format_datetime(sent_at)
        message_id = message_id or make_msgid(domain=sender.rsplit("@", 1)[1])
        message["Message-ID"] = message_id
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
        if references:
            message["References"] = " ".join(references[-20:])
        if not in_reply_to:
            message["List-Unsubscribe"] = f"<mailto:{sender}?subject=unsubscribe>"
        if delivery_id is not None:
            message["X-FuelLead-Delivery-ID"] = str(delivery_id)
        if campaign_id is not None:
            message["X-FuelLead-Campaign-ID"] = str(campaign_id)
        # Always emit CRLF and a 7-bit-safe body; bytes passed to SMTP.data
        # are not normalized and BODY=8BITMIME is not negotiated here.
        message.set_content(text_body, cte="quoted-printable")

        raw_message = message.as_bytes()
        smtp = self._connect()
        data_started = False
        accepted = None
        try:
            code, response = smtp.mail(sender)
            if not 200 <= code < 300:
                raise _rejection(code, response, stage="MAIL FROM", password=self.password)
            code, response = smtp.rcpt(target)
            if not 200 <= code < 300:
                raise _rejection(code, response, stage="RCPT", password=self.password, recipient=target)
            data_started = True
            code, response = smtp.data(raw_message)
            if not 200 <= code < 300:
                raise _rejection(code, response, stage="DATA", password=self.password, recipient=target)
            accepted = SMTPAccepted(
                message_id=message_id,
                smtp_code=smtp_status_code(code, response) or str(code),
                smtp_response=safe_smtp_text(response, secret=self.password),
            )
        except SMTPDeliveryError:
            raise
        except smtplib.SMTPDataError as exc:
            # Explicit refusal before 354 is known not accepted.
            raise _rejection(exc.smtp_code, exc.smtp_error, stage="DATA", password=self.password, recipient=target) from exc
        except (smtplib.SMTPServerDisconnected, OSError) as exc:
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
