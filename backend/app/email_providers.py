from sqlalchemy import ColumnElement, func, not_, or_

from app.models import CompanyEmail


EMAIL_PROVIDER_DOMAINS: dict[str, tuple[str, ...]] = {
    "yandex": ("yandex.ru", "ya.ru", "yandex.com", "yandex.by", "yandex.kz"),
    "google": ("gmail.com", "googlemail.com"),
    "mail_ru": ("mail.ru", "bk.ru", "inbox.ru", "list.ru", "internet.ru"),
    "rambler": ("rambler.ru", "lenta.ru", "autorambler.ru", "ro.ru", "myrambler.ru"),
}
EMAIL_PROVIDER_VALUES = (*EMAIL_PROVIDER_DOMAINS, "other")


def _matches_domains(domains: tuple[str, ...]) -> ColumnElement[bool]:
    normalized_email = func.lower(func.trim(CompanyEmail.email))
    return or_(*(normalized_email.like(f"%@{domain}") for domain in domains))


def email_provider_predicate(provider: str) -> ColumnElement[bool]:
    """Return an email-row predicate for one public provider group."""
    if provider == "other":
        known_domains = tuple(
            domain
            for domains in EMAIL_PROVIDER_DOMAINS.values()
            for domain in domains
        )
        return not_(_matches_domains(known_domains))
    return _matches_domains(EMAIL_PROVIDER_DOMAINS[provider])
