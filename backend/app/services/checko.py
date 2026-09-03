import re
from typing import Any

import httpx

from app.services.provider import (
    CompanyPayload,
    DiscoveryAPIError,
    OkvedItem,
    SearchPage,
    normalize_email,
    redact_sensitive_url,
)


class CheckoAPIError(DiscoveryAPIError):
    pass


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
    raw_phones = contacts.get("Тел") or [] if isinstance(contacts, dict) else []
    if isinstance(raw_phones, str):
        raw_phones = [raw_phones]
    from app.services.contacts import normalize_phone

    phone_numbers = sorted(
        {
            normalized
            for value in raw_phones
            if isinstance(value, str) and (normalized := normalize_phone(value))
        }
    )

    status_raw = data.get("Статус") or ""
    status_name = status_raw.get("Наим", "") if isinstance(status_raw, dict) else str(status_raw)
    status_normalized = status_name.casefold().strip()
    is_active = "действ" in status_normalized and not status_normalized.startswith("не действ")

    region_raw = data.get("Регион") or {}
    region_code = None
    region_name = None
    if isinstance(region_raw, dict):
        if region_raw.get("Код") is not None:
            region_code = str(region_raw["Код"]).strip() or None
        if region_raw.get("Наим") is not None:
            region_name = str(region_raw["Наим"]).strip() or None

    return CompanyPayload(
        name=data.get("НаимСокр") or data.get("НаимПолн") or "Без наименования",
        inn=str(data.get("ИНН") or ""),
        ogrn=str(data["ОГРН"]) if data.get("ОГРН") else None,
        primary_okved=primary,
        additional_okveds=list(additional_by_code.values()),
        emails=emails,
        phone_numbers=phone_numbers,
        is_active=is_active,
        region_code=region_code,
        region_name=region_name,
    )


class CheckoClient:
    fixed_page_size = None

    def __init__(
        self,
        api_keys: str | list[str] | tuple[str, ...],
        base_url: str,
        timeout_seconds: float = 30.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        raw_keys = [api_keys] if isinstance(api_keys, str) else list(api_keys)
        self.api_keys = tuple(dict.fromkeys(key.strip() for key in raw_keys if key.strip()))
        if not self.api_keys:
            raise ValueError("Checko API key is required")
        self.active_key_index = 0
        self.unavailable_key_reasons: dict[int, str] = {}
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
        )

    def __enter__(self) -> "CheckoClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.client.close()

    @property
    def api_key_count(self) -> int:
        return len(self.api_keys)

    def _request(self, path: str, params: dict[str, Any], api_key: str) -> httpx.Response:
        try:
            return self.client.get(path, params={"key": api_key, **params})
        except httpx.TimeoutException as exc:
            raise CheckoAPIError(
                "Checko не ответил вовремя. Продолжаем поиск через следующий доступный провайдер.",
                stop_discovery=True,
                reason="timeout",
            ) from exc
        except httpx.RequestError as exc:
            raise CheckoAPIError(
                "Не удалось связаться с Checko. Продолжаем поиск через следующий доступный провайдер.",
                stop_discovery=True,
                reason="connection_error",
            ) from exc

    @staticmethod
    def _response_error(response: httpx.Response, payload: dict[str, Any]) -> CheckoAPIError | None:
        meta = payload.get("meta") or {}
        provider_message = str(meta.get("message") or "").strip()
        message_normalized = provider_message.casefold()

        daily_limit = "суточн" in message_normalized and "лимит" in message_normalized
        if daily_limit:
            request_count = meta.get("today_request_count")
            usage = f" Сегодня использовано {request_count} из 100 запросов." if request_count is not None else ""
            return CheckoAPIError(
                "Суточный лимит Checko исчерпан."
                f"{usage} Поиск станет доступен после обновления лимита или пополнения баланса.",
                stop_discovery=True,
                key_unavailable=True,
                reason="daily_limit",
            )

        if response.status_code in (401, 403):
            return CheckoAPIError(
                "Checko отклонил API-ключ. Проверьте ключ и доступ к методу поиска в личном кабинете Checko.",
                stop_discovery=True,
                key_unavailable=True,
                reason="invalid_key",
            )
        if response.status_code == 429:
            return CheckoAPIError(
                "Checko временно ограничил частоту запросов. Повторите поиск позже.",
                stop_discovery=True,
                reason="rate_limit",
            )
        if response.is_error:
            return CheckoAPIError(provider_message or f"Checko вернул ошибку HTTP {response.status_code}.")
        if meta.get("status") == "error":
            return CheckoAPIError(provider_message or "Checko не смог обработать запрос")
        return None

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        for key_index in range(self.active_key_index, len(self.api_keys)):
            response = self._request(path, params, self.api_keys[key_index])

            try:
                payload = response.json()
            except ValueError:
                payload = {}

            error = self._response_error(response, payload)
            if error is not None:
                if error.key_unavailable:
                    self.unavailable_key_reasons[key_index] = error.reason or "unavailable"
                    if key_index + 1 < len(self.api_keys):
                        self.active_key_index = key_index + 1
                        continue
                if len(self.api_keys) == 1:
                    raise error
                if len(self.unavailable_key_reasons) == len(self.api_keys):
                    all_daily_limits = all(
                        reason == "daily_limit"
                        for reason in self.unavailable_key_reasons.values()
                    )
                    message = (
                        "Суточный лимит Checko исчерпан на всех настроенных API-ключах."
                        if all_daily_limits
                        else "Все настроенные API-ключи Checko недоступны. Проверьте лимиты и ключи."
                    )
                    raise CheckoAPIError(message, stop_discovery=True)
                raise error

            self.active_key_index = key_index
            data = payload.get("data")
            if not isinstance(data, dict):
                raise CheckoAPIError("Checko вернул ответ без данных")
            return data

        raise CheckoAPIError("Все настроенные API-ключи Checko недоступны.", stop_discovery=True)

    def search_by_okved(
        self,
        code: str,
        *,
        region_code: str,
        limit: int = 10,
        page: int = 1,
    ) -> SearchPage:
        if not re.fullmatch(r"\d{2}", region_code):
            raise ValueError("Checko region code must contain exactly two digits")
        data = self._get(
            "/search",
            {
                "by": "okved",
                "obj": "org",
                "query": code,
                "active": "true",
                "codes": "all",
                "region": region_code,
                "limit": min(max(limit, 1), 100),
                "page": page,
            },
        )
        records = data.get("Записи") or []
        try:
            current_page = max(int(data.get("СтрТекущ") or page), 1)
        except (TypeError, ValueError):
            current_page = max(page, 1)
        try:
            total_pages = max(int(data.get("СтрВсего") or current_page), 1)
        except (TypeError, ValueError):
            total_pages = current_page
        return SearchPage(
            records=[item for item in records if isinstance(item, dict)],
            current_page=current_page,
            total_pages=total_pages,
        )

    def get_company(self, inn: str) -> CompanyPayload:
        return parse_company_payload(self._get("/company", {"inn": inn}))
