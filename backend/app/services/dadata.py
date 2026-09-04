"""DaData suggestions and INN lookup, not a paginated bulk export."""
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.services.contacts import normalize_phone
from app.services.provider import CompanyPayload, DiscoveryAPIError, OkvedItem, SearchPage, normalize_email


def parse_dadata_company_payload(suggestion: dict[str, Any]) -> CompanyPayload:
    data = suggestion.get("data") or {}
    inn = str(data.get("inn") or "").strip()
    if data.get("type") != "LEGAL" or not re.fullmatch(r"\d{10}", inn):
        raise ValueError("DaData: карточка не содержит ИНН юридического лица.")
    address = (data.get("address") or {}).get("data") or {}
    region_id = str(address.get("region_kladr_id") or address.get("kladr_id") or "")
    region_code = region_id[:2] if re.fullmatch(r"\d{13,19}", region_id) else None
    name = data.get("name") or {}
    primary_code = str(data.get("okved") or "").strip()
    additional = {
        str(item["code"]): OkvedItem(str(item["code"]), item.get("name"))
        for item in data.get("okveds") or []
        if isinstance(item, dict) and item.get("code") and not item.get("main") and str(item["code"]) != primary_code
    }
    emails, phones = set(), set()
    for item in data.get("emails") or []:
        value = item.get("value") if isinstance(item, dict) else item
        if isinstance(value, str) and (email := normalize_email(value)):
            emails.add(email)
    for item in data.get("phones") or []:
        details = item.get("data") or {} if isinstance(item, dict) else {}
        value = details.get("source") or (item.get("value") if isinstance(item, dict) else item)
        if isinstance(value, str) and (phone := normalize_phone(value)):
            phones.add(phone)
    return CompanyPayload(
        name=str(name.get("short_with_opf") or name.get("full_with_opf") or suggestion.get("value") or "Без наименования"),
        inn=inn, ogrn=str(data.get("ogrn") or "").strip() or None,
        primary_okved=OkvedItem(primary_code) if primary_code else None,
        additional_okveds=list(additional.values()), emails=sorted(emails), phone_numbers=sorted(phones),
        is_active=(data.get("state") or {}).get("status") == "ACTIVE",
        region_code=region_code, region_name=address.get("region_with_type"),
    )


class DaDataClient:
    # Stable size allows a saved record cursor to consume the full suggestion batch.
    # DaData does not accept page/offset and returns at most 20 suggestions.
    fixed_page_size = 20

    def __init__(self, api_key: str, base_url: str = "https://suggestions.dadata.ru/suggestions/api/4_1/rs",
                 timeout_seconds: float = 30.0, *, secret_key: str = "", transport: httpx.BaseTransport | None = None):
        if not api_key.strip():
            raise ValueError("DaData API key is required")
        self.secret_key = secret_key.strip()
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_seconds,
                                   headers={"Authorization": f"Token {api_key.strip()}", "Accept": "application/json"}, transport=transport)

    def __enter__(self):
        return self

    def __exit__(self, *_: object):
        self.client.close()

    def _daily_quota_exhausted(self) -> bool:
        # 403 alone also means a bad token or unconfirmed email. Verify the actual
        # daily remaining balance; do not send X-Secret to suggestion endpoints.
        if not self.secret_key:
            return False
        try:
            response = self.client.get("https://dadata.ru/api/v2/stat/daily", headers={"X-Secret": self.secret_key})
            if response.status_code != 200:
                return False
            payload = response.json()
            remaining = (payload.get("remaining") or {}).get("suggestions")
            today = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()
            return payload.get("date") == today and isinstance(remaining, (int, float)) and not isinstance(remaining, bool) and remaining <= 0
        except (httpx.RequestError, ValueError, AttributeError, TypeError):
            return False

    def _post(self, path: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            response = self.client.post(path, json=body)
        except httpx.RequestError:
            raise DiscoveryAPIError("DaData временно недоступна. Повторите поиск позже.",
                                    stop_discovery=True, reason="connection_error") from None
        if response.status_code == 403 and self._daily_quota_exhausted():
            raise DiscoveryAPIError("Суточный лимит DaData исчерпан.", stop_discovery=True, reason="daily_limit")
        if response.status_code in (401, 403):
            raise DiscoveryAPIError("DaData отклонила доступ. Проверьте ключ, подтверждение почты и лимит в кабинете.",
                                    stop_discovery=True, reason="access_denied")
        if response.status_code == 429:
            raise DiscoveryAPIError("DaData ограничила частоту запросов. Повторите поиск позже.",
                                    stop_discovery=True, reason="rate_limit")
        if response.is_error:
            raise DiscoveryAPIError(f"DaData вернула ошибку HTTP {response.status_code}.",
                                    stop_discovery=True, reason="http_error")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or not isinstance(payload.get("suggestions"), list):
            raise DiscoveryAPIError("DaData вернула ответ без корректных данных.",
                                    stop_discovery=True, reason="invalid_response")
        return [item for item in payload["suggestions"] if isinstance(item, dict)]

    def search_by_okved(self, code: str, *, region_code: str, limit: int = 10, page: int = 1) -> SearchPage:
        if region_code not in ("77", "50"):
            raise ValueError("DaData: неподдерживаемый регион поиска.")
        suggestions = self._post("/suggest/party", {
            "query": "Москва" if region_code == "77" else "Московская обл",
            "count": self.fixed_page_size, "type": "LEGAL", "status": ["ACTIVE"],
            "okved": [code], "locations": [{"kladr_id": region_code + "0" * 11}],
        })
        records = []
        for item in suggestions:
            data = item.get("data") or {}
            if data.get("type") != "LEGAL" or not re.fullmatch(r"\d{10}", str(data.get("inn") or "")):
                continue
            company = parse_dadata_company_payload(item)
            records.append({"ИНН": company.inn, "РегионКод": company.region_code or ""})
        return SearchPage(records, current_page=1, total_pages=1)

    def get_company(self, inn: str) -> CompanyPayload:
        if not re.fullmatch(r"\d{10}", inn):
            raise ValueError("DaData: некорректный ИНН.")
        suggestions = self._post("/findById/party", {"query": inn, "count": 1, "type": "LEGAL", "branch_type": "MAIN"})
        for item in suggestions:
            data = item.get("data") or {}
            if str(data.get("inn")) == inn and data.get("branch_type") == "MAIN":
                return parse_dadata_company_payload(item)
        raise DiscoveryAPIError("DaData не вернула головную организацию с запрошенным ИНН.", reason="not_found")
