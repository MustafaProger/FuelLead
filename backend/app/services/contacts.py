import re
from urllib.parse import urlparse

from app.models import CONTACT_TYPES


CONTACT_TYPE_LABELS = {
    "phone": "Телефон",
    "whatsapp": "WhatsApp",
    "telegram": "Telegram",
}
PHONE_DIGITS_PATTERN = re.compile(r"^\d{7,15}$")
TELEGRAM_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def normalize_phone(value: str) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    digits = "".join(character for character in raw if character.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = f"7{digits[1:]}"
    elif len(digits) == 10:
        digits = f"7{digits}"
    if not PHONE_DIGITS_PATTERN.fullmatch(digits):
        return None
    return f"+{digits}"


def _whatsapp_phone(value: str) -> str:
    raw = value.strip()
    candidate = raw
    if "://" in raw:
        parsed = urlparse(raw)
        host = parsed.netloc.casefold().removeprefix("www.")
        if host == "wa.me":
            candidate = parsed.path.strip("/").split("/", 1)[0]
        elif host in {"api.whatsapp.com", "whatsapp.com"}:
            candidate = dict(
                pair.split("=", 1) for pair in parsed.query.split("&") if "=" in pair
            ).get("phone", "")
    return candidate


def _telegram_username(value: str) -> str:
    raw = value.strip()
    if "://" in raw:
        parsed = urlparse(raw)
        host = parsed.netloc.casefold().removeprefix("www.")
        if host not in {"t.me", "telegram.me"}:
            return ""
        raw = parsed.path.strip("/").split("/", 1)[0]
    return raw.removeprefix("@").strip()


def normalize_contact_value(contact_type: str, value: str) -> str:
    if contact_type not in CONTACT_TYPES:
        raise ValueError("Недоступный тип контакта")

    if contact_type == "phone":
        normalized = normalize_phone(value)
        if normalized is None:
            raise ValueError("Введите телефон с кодом страны, например +7 999 123-45-67")
        return normalized

    if contact_type == "whatsapp":
        normalized = normalize_phone(_whatsapp_phone(value))
        if normalized is None:
            raise ValueError("Введите номер WhatsApp с кодом страны")
        return normalized

    username = _telegram_username(value)
    if not TELEGRAM_USERNAME_PATTERN.fullmatch(username):
        raise ValueError("Введите Telegram username, например @company_name")
    return f"@{username.lower()}"


def contact_href(contact_type: str, value: str) -> str:
    if contact_type == "phone":
        return f"tel:{value}"
    if contact_type == "whatsapp":
        return f"https://wa.me/{value.removeprefix('+')}"
    return f"https://t.me/{value.removeprefix('@')}"
