import re
from dataclasses import dataclass, field
from typing import Any, Protocol


class DiscoveryAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stop_discovery: bool = False,
        key_unavailable: bool = False,
        reason: str | None = None,
    ):
        super().__init__(message)
        self.stop_discovery = stop_discovery
        self.key_unavailable = key_unavailable
        self.reason = reason


@dataclass(slots=True)
class OkvedItem:
    code: str
    name: str | None = None


@dataclass(slots=True)
class CompanyPayload:
    name: str
    inn: str
    ogrn: str | None
    primary_okved: OkvedItem | None
    additional_okveds: list[OkvedItem] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phone_numbers: list[str] = field(default_factory=list)
    is_active: bool = True
    region_code: str | None = None
    region_name: str | None = None


@dataclass(slots=True)
class SearchPage:
    records: list[dict[str, Any]]
    current_page: int
    total_pages: int


class DiscoveryClient(Protocol):
    fixed_page_size: int | None

    def search_by_okved(
        self,
        code: str,
        *,
        region_code: str,
        limit: int = 10,
        page: int = 1,
    ) -> SearchPage: ...

    def get_company(self, inn: str) -> CompanyPayload: ...


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
SECRET_QUERY_PATTERN = re.compile(r"([?&]key=)[^&\s'\"]+", re.IGNORECASE)


def redact_sensitive_url(value: str) -> str:
    return SECRET_QUERY_PATTERN.sub(r"\1<redacted>", value)


def normalize_email(value: str) -> str | None:
    normalized = value.strip().lower().removeprefix("mailto:")
    return normalized if EMAIL_PATTERN.match(normalized) else None
