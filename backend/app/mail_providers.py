"""Connection presets for sender mailboxes (separate from recipient filters)."""
from dataclasses import dataclass


@dataclass(frozen=True)
class MailProvider:
    label: str
    domains: tuple[str, ...]
    smtp_host: str
    imap_host: str
    smtp_port: int = 465
    imap_port: int = 993
    automatic_sent_copy: bool = False


MAIL_PROVIDERS = {
    "mailru_smtp": MailProvider(
        "Mail.ru", ("mail.ru", "bk.ru", "inbox.ru", "list.ru", "internet.ru"),
        "smtp.mail.ru", "imap.mail.ru",
    ),
    "gmail_smtp": MailProvider(
        "Gmail", ("gmail.com", "googlemail.com"),
        "smtp.gmail.com", "imap.gmail.com", automatic_sent_copy=True,
    ),
    "yandex_smtp": MailProvider(
        "Яндекс Почта", ("yandex.ru", "ya.ru", "yandex.com", "yandex.by", "yandex.kz", "yandex.uz", "yandex.com.tr"),
        "smtp.yandex.ru", "imap.yandex.ru",
    ),
}
SMTP_SENDER_PROVIDERS = tuple(MAIL_PROVIDERS)
