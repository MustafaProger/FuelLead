import re
from dataclasses import dataclass, field
from typing import Any

import httpx


class CheckoAPIError(RuntimeError):
    def __init__(self, message: str, *, stop_discovery: bool = False):
        super().__init__(message)
        self.stop_discovery = stop_discovery


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
    is_active: bool = True


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
SECRET_QUERY_PATTERN = re.compile(r"([?&]key=)[^&\s'\"]+", re.IGNORECASE)


def redact_sensitive_url(value: str) -> str:
    return SECRET_QUERY_PATTERN.sub(r"\1<redacted>", value)


def normalize_email(value: str) -> str | None:
    normalized = value.strip().lower().removeprefix("mailto:")
    return normalized if EMAIL_PATTERN.match(normalized) else None


def parse_company_payload(data: dict[str, Any]) -> CompanyPayload:
    primary_raw = data.get("ОКВЭД") or {}
    primary = None
    if isinstance(primary_raw, dict) and primary_raw.get("Код"):
        primary = OkvedItem(str(primary_raw["Код"]), primary_raw.get("Наим"))

    additional_by_code: dict[str, OkvedItem] = {}
    for item in data.get("ОКВЭДДоп") or []:
        if isinstance(item, dict) and item.get("Код"):
            code = str(item["Код"]).strip()
            if not code:
                continue
            existing = additional_by_code.get(code)
            if existing is None:
                additional_by_code[code] = OkvedItem(code, item.get("Наим"))
            elif not existing.name and item.get("Наим"):
                existing.name = item["Наим"]

    contacts = data.get("Контакты") or {}
    raw_emails = contacts.get("Емэйл") or [] if isinstance(contacts, dict) else []
    if isinstance(raw_emails, str):
        raw_emails = [raw_emails]
    emails = sorted(
        {
            normalized
            for value in raw_emails
            if isinstance(value, str) and (normalized := normalize_email(value))
        }
    )

    status_raw = data.get("Статус") or ""
    status_name = status_raw.get("Наим", "") if isinstance(status_raw, dict) else str(status_raw)
    status_normalized = status_name.casefold().strip()
    is_active = "действ" in status_normalized and not status_normalized.startswith("не действ")

    return CompanyPayload(
        name=data.get("НаимСокр") or data.get("НаимПолн") or "Без наименования",
        inn=str(data.get("ИНН") or ""),
        ogrn=str(data["ОГРН"]) if data.get("ОГРН") else None,
        primary_okved=primary,
        additional_okveds=list(additional_by_code.values()),
        emails=emails,
        is_active=is_active,
    )


class CheckoClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout_seconds: float = 30.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        if not api_key.strip():
            raise ValueError("Checko API key is required")
        self.api_key = api_key
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
        )

    def __enter__(self) -> "CheckoClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.client.close()

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.client.get(path, params={"key": self.api_key, **params})
        except httpx.TimeoutException as exc:
            raise CheckoAPIError("Checko не ответил вовремя. Повторите поиск позже.") from exc
        except httpx.RequestError as exc:
            raise CheckoAPIError("Не удалось связаться с Checko. Проверьте подключение и повторите поиск.") from exc

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        meta = payload.get("meta") or {}
        provider_message = str(meta.get("message") or "").strip()

        if response.is_error:
            if response.status_code == 403 and "суточн" in provider_message.casefold():
                request_count = meta.get("today_request_count")
                usage = f" Сегодня использовано {request_count} из 100 запросов." if request_count is not None else ""
                raise CheckoAPIError(
                    "Суточный лимит Checko исчерпан."
                    f"{usage} Поиск станет доступен после обновления лимита или пополнения баланса.",
                    stop_discovery=True,
                )
            if response.status_code in (401, 403):
                raise CheckoAPIError(
                    "Checko отклонил API-ключ. Проверьте ключ и доступ к методу поиска в личном кабинете Checko.",
                    stop_discovery=True,
                )
            if response.status_code == 429:
                raise CheckoAPIError(
                    "Checko временно ограничил частоту запросов. Повторите поиск позже.",
                    stop_discovery=True,
                )
            raise CheckoAPIError(provider_message or f"Checko вернул ошибку HTTP {response.status_code}.")

        if meta.get("status") == "error":
            raise CheckoAPIError(provider_message or "Checko не смог обработать запрос")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise CheckoAPIError("Checko вернул ответ без данных")
        return data

    def search_by_okved(self, code: str, limit: int = 10, page: int = 1) -> list[dict[str, Any]]:
        data = self._get(
            "/search",
            {
                "by": "okved",
                "obj": "org",
                "query": code,
                "active": "true",
                "codes": "all",
                "limit": min(max(limit, 1), 100),
                "page": page,
            },
        )
        records = data.get("Записи") or []
        return [item for item in records if isinstance(item, dict)]

    def get_company(self, inn: str) -> CompanyPayload:
        return parse_company_payload(self._get("/company", {"inn": inn}))
