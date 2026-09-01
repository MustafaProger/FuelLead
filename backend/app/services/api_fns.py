import json
import math
import re
from typing import Any

import httpx

from app.services.contacts import normalize_phone
from app.services.provider import (
    CompanyPayload,
    DiscoveryAPIError,
    OkvedItem,
    SearchPage,
    normalize_email,
    redact_sensitive_url,
)


class ApiFnsAPIError(DiscoveryAPIError):
    pass


def _string_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,;\n]+", value) if item.strip()]
    return []


def _region_name(address: dict[str, Any]) -> str | None:
    details = address.get("АдресДетали") or {}
    if isinstance(details, dict):
        region = details.get("Регион") or {}
        if isinstance(region, dict):
            name = str(region.get("Наим") or "").strip()
            if name:
                return name
        elif region:
            return str(region).strip() or None
    full_address = str(address.get("АдресПолн") or "").strip()
    return full_address or None


def _active_status(value: Any) -> bool:
    if isinstance(value, dict):
        value = value.get("Наим") or value.get("Текст") or ""
    normalized = str(value or "").casefold().strip()
    return "действующ" in normalized and "недейств" not in normalized


def _truthy(value: Any) -> bool:
    return value is True or str(value).casefold().strip() in {"1", "true", "yes", "да"}


def parse_api_fns_company_payload(data: dict[str, Any]) -> CompanyPayload:
    primary_raw = data.get("ОснВидДеят") or {}
    primary = None
    if isinstance(primary_raw, dict):
        code = str(primary_raw.get("Код") or "").strip()
        if code:
            primary = OkvedItem(code=code, name=str(primary_raw.get("Текст") or "").strip() or None)

    additional_by_code: dict[str, OkvedItem] = {}
    additional_raw = data.get("ДопВидДеят") or []
    if isinstance(additional_raw, dict):
        additional_raw = [additional_raw]
    for item in additional_raw:
        if not isinstance(item, dict):
            continue
        code = str(item.get("Код") or "").strip()
        if not code:
            continue
        name = str(item.get("Текст") or "").strip() or None
        existing = additional_by_code.get(code)
        if existing is None:
            additional_by_code[code] = OkvedItem(code=code, name=name)
        elif not existing.name and name:
            existing.name = name

    contacts = data.get("Контакты") or {}
    if not isinstance(contacts, dict):
        contacts = {}
    raw_emails: list[str] = []
    for key in ("e-mail", "email", "Email", "Емэйл"):
        raw_emails.extend(_string_values(contacts.get(key)))
    emails = sorted(
        {
            normalized
            for value in raw_emails
            if (normalized := normalize_email(value)) is not None
        }
    )
    raw_phones: list[str] = []
    for key in ("Телефон", "Тел", "phone"):
        raw_phones.extend(_string_values(contacts.get(key)))
    phone_numbers = sorted(
        {
            normalized
            for value in raw_phones
            if (normalized := normalize_phone(value)) is not None
        }
    )

    address = data.get("Адрес") or {}
    if not isinstance(address, dict):
        address = {}
    raw_region_code = str(address.get("КодРегион") or "").strip()
    region_code = raw_region_code.zfill(2) if raw_region_code.isdigit() else None

    return CompanyPayload(
        name=str(data.get("НаимСокрЮЛ") or data.get("НаимПолнЮЛ") or "Без наименования"),
        inn=str(data.get("ИНН") or "").strip(),
        ogrn=str(data.get("ОГРН") or "").strip() or None,
        primary_okved=primary,
        additional_okveds=list(additional_by_code.values()),
        emails=emails,
        phone_numbers=phone_numbers,
        is_active=_active_status(data.get("Статус")),
        region_code=region_code,
        region_name=_region_name(address),
    )


def parse_api_fns_egr_response(payload: dict[str, Any]) -> CompanyPayload:
    for wrapper in payload.get("items") or []:
        if isinstance(wrapper, dict) and isinstance(wrapper.get("ЮЛ"), dict):
            return parse_api_fns_company_payload(wrapper["ЮЛ"])
    raise ApiFnsAPIError("API-ФНС не вернул карточку юридического лица.")


class ApiFnsClient:
    fixed_page_size = 100

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api-fns.ru/api",
        timeout_seconds: float = 30.0,
        *,
        require_phone: bool = False,
        require_email: bool = False,
        transport: httpx.BaseTransport | None = None,
    ):
        self.api_key = api_key.strip()
        if not self.api_key:
            raise ValueError("API-ФНС key is required")
        self.require_phone = require_phone
        self.require_email = require_email
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
        )

    def __enter__(self) -> "ApiFnsClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.client.close()

    def _safe_provider_message(self, payload: dict[str, Any]) -> str:
        raw = payload.get("error") or payload.get("Ошибка") or payload.get("message") or ""
        if isinstance(raw, (dict, list)):
            raw = json.dumps(raw, ensure_ascii=False)
        return redact_sensitive_url(str(raw).replace(self.api_key, "<redacted>")).strip()

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.client.get(path, params={**params, "key": self.api_key})
        except httpx.TimeoutException as exc:
            raise ApiFnsAPIError(
                "API-ФНС не ответил вовремя. Проверьте доступность сервиса и повторите поиск позже.",
                stop_discovery=True,
                reason="timeout",
            ) from exc
        except httpx.RequestError as exc:
            raise ApiFnsAPIError(
                "Не удалось установить защищённое соединение с API-ФНС. Проверьте сеть и доступ с разрешённого IP-адреса.",
                stop_discovery=True,
                reason="connection_error",
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            if response.status_code == 401:
                raise ApiFnsAPIError(
                    "API-ФНС отклонил ключ. Проверьте API_FNS_KEY в локальном .env.",
                    stop_discovery=True,
                    key_unavailable=True,
                    reason="invalid_key",
                ) from exc
            if response.status_code == 403:
                raise ApiFnsAPIError(
                    "API-ФНС отклонил доступ. Проверьте ключ, лимит метода и привязанный IP-адрес.",
                    stop_discovery=True,
                    reason="access_denied",
                ) from exc
            if response.status_code == 429:
                raise ApiFnsAPIError(
                    "API-ФНС временно ограничил частоту запросов. Повторите поиск позже.",
                    stop_discovery=True,
                    reason="rate_limit",
                ) from exc
            if response.is_error:
                raise ApiFnsAPIError(
                    f"API-ФНС вернул ошибку HTTP {response.status_code}.",
                    stop_discovery=True,
                    reason="http_error",
                ) from exc
            raise ApiFnsAPIError(
                "API-ФНС вернул ответ в неподдерживаемом формате.",
                stop_discovery=True,
                reason="invalid_response",
            ) from exc
        if not isinstance(payload, dict):
            if response.status_code == 401:
                raise ApiFnsAPIError(
                    "API-ФНС отклонил ключ. Проверьте API_FNS_KEY в локальном .env.",
                    stop_discovery=True,
                    key_unavailable=True,
                    reason="invalid_key",
                )
            if response.status_code == 403:
                raise ApiFnsAPIError(
                    "API-ФНС отклонил доступ. Проверьте ключ, лимит метода и привязанный IP-адрес.",
                    stop_discovery=True,
                    reason="access_denied",
                )
            if response.status_code == 429:
                raise ApiFnsAPIError(
                    "API-ФНС временно ограничил частоту запросов. Повторите поиск позже.",
                    stop_discovery=True,
                    reason="rate_limit",
                )
            if response.is_error:
                raise ApiFnsAPIError(
                    f"API-ФНС вернул ошибку HTTP {response.status_code}.",
                    stop_discovery=True,
                    reason="http_error",
                )
            raise ApiFnsAPIError(
                "API-ФНС вернул ответ в неподдерживаемом формате.",
                stop_discovery=True,
                reason="invalid_response",
            )

        message = self._safe_provider_message(payload)
        normalized = message.casefold()
        if response.status_code == 401 or (
            "ключ" in normalized
            and any(
                word in normalized
                for word in ("не действ", "недейств", "невер", "не вер", "invalid")
            )
        ):
            raise ApiFnsAPIError(
                "API-ФНС отклонил ключ. Проверьте API_FNS_KEY в локальном .env.",
                stop_discovery=True,
                key_unavailable=True,
                reason="invalid_key",
            )
        if any(
            word in normalized
            for word in ("ip-адрес", "ip адрес", "ip-address", "ip address", "айпи")
        ):
            raise ApiFnsAPIError(
                "API-ФНС отклонил запрос из-за ограничения IP. Используйте IP-адрес, привязанный к ключу.",
                stop_discovery=True,
                key_unavailable=True,
                reason="ip_restriction",
            )
        if any(word in normalized for word in ("лимит", "исчерпан", "превышено количество")):
            raise ApiFnsAPIError(
                "Лимит запросов API-ФНС исчерпан. Проверьте /stat и дождитесь обновления или измените тариф.",
                stop_discovery=True,
                reason="quota_exhausted",
            )
        if response.status_code == 429:
            raise ApiFnsAPIError(
                "API-ФНС временно ограничил частоту запросов. Повторите поиск позже.",
                stop_discovery=True,
                reason="rate_limit",
            )
        if response.status_code == 403:
            raise ApiFnsAPIError(
                "API-ФНС отклонил доступ. Проверьте ключ, лимит метода и привязанный IP-адрес.",
                stop_discovery=True,
                reason="access_denied",
            )
        if response.is_error:
            raise ApiFnsAPIError(
                f"API-ФНС вернул ошибку HTTP {response.status_code}.",
                stop_discovery=True,
                reason="http_error",
            )
        if message:
            raise ApiFnsAPIError(
                f"API-ФНС не смог обработать запрос: {message[:300]}",
                stop_discovery=True,
                reason="provider_error",
            )
        return payload

    def search_by_okved(
        self,
        code: str,
        *,
        region_code: str,
        limit: int = 10,
        page: int = 1,
    ) -> SearchPage:
        del limit  # API-ФНС задаёт размер страницы на стороне сервиса (до 100 записей).
        if not re.fullmatch(r"\d{2}", region_code):
            raise ValueError("API-ФНС region code must contain exactly two digits")
        page = max(page, 1)
        filters = ["active", "onlyul", f"okved{code}", f"region{region_code}"]
        if self.require_phone:
            filters.append("withphone")
        if self.require_email:
            filters.append("withemail")
        payload = self._request(
            "/search",
            {"q": "any", "filter": "+".join(filters), "page": page},
        )

        records: list[dict[str, Any]] = []
        for wrapper in payload.get("items") or []:
            if not isinstance(wrapper, dict) or not isinstance(wrapper.get("ЮЛ"), dict):
                continue
            item = wrapper["ЮЛ"]
            address = item.get("Адрес") or {}
            raw_region = address.get("КодРегион") if isinstance(address, dict) else None
            records.append(
                {
                    "ИНН": str(item.get("ИНН") or "").strip(),
                    "ОГРН": str(item.get("ОГРН") or "").strip(),
                    "РегионКод": str(raw_region or "").strip(),
                }
            )

        total_raw = payload.get("filter_any_count")
        try:
            total_count = max(int(total_raw), 0)
        except (TypeError, ValueError):
            total_count = 0
        if total_count:
            total_pages = max(math.ceil(total_count / self.fixed_page_size), page)
        elif _truthy(payload.get("nextpage")):
            total_pages = page + 1
        else:
            total_pages = page
        return SearchPage(records=records, current_page=page, total_pages=total_pages)

    def get_company(self, inn: str) -> CompanyPayload:
        return parse_api_fns_egr_response(self._request("/egr", {"req": inn}))

    def get_statistics(self) -> dict[str, Any]:
        return self._request("/stat", {})
