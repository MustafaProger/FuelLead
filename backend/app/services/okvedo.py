"""Okvedo company search and cards: https://okvedo.ru/api/v1/docs."""
import re
from typing import Any

import httpx

from app.services.contacts import normalize_phone
from app.services.provider import CompanyPayload, DiscoveryAPIError, OkvedItem, SearchPage, normalize_email

REGION_NAMES = {"77": "Москва", "50": "Московская область"}

# OKVED2 class 01 groups. Okvedo does not expand the class query into these
# groups (verified 2026-09-29), although e.g. 01.11 has companies.
# Keep the original query as well; do not infer coverage of descendants from
# an empty parent query or silently broaden the user's selected activities.
AGRICULTURE_GROUP_CODES = (
    "01.11", "01.12", "01.13", "01.14", "01.15", "01.16", "01.19",
    "01.21", "01.22", "01.23", "01.24", "01.25", "01.26", "01.27", "01.28", "01.29",
    "01.30", "01.41", "01.42", "01.43", "01.44", "01.45", "01.46", "01.47", "01.49",
    "01.50", "01.61", "01.62", "01.63", "01.64", "01.70",
)


def normalize_okved(value: Any) -> str:
    code = str(value or "").strip()
    # Some Okvedo rows store the six-digit agricultural code as a number:
    # 011100 becomes 11100. Restore only evidenced class-01 groups, leaving
    # ordinary five-digit codes such as 49411 unchanged.
    if code.isdigit() and len(code) == 5:
        padded = code.zfill(6)
        if f"{padded[:2]}.{padded[2:4]}" in AGRICULTURE_GROUP_CODES:
            code = padded
    if code.isdigit() and len(code) > 2:
        return ".".join(code[i:i + 2] for i in range(0, len(code), 2))
    return code


def _region(data: dict[str, Any]) -> tuple[str | None, str | None]:
    # Never infer the current address from an INN prefix or the requested filter.
    addresses = [a for a in data.get("addresses") or [] if isinstance(a, dict)]
    for item in [data, *addresses]:
        code = str(item.get("region_code") or "").strip()
        if re.fullmatch(r"\d{1,2}", code):
            return code.zfill(2), item.get("region")
        name = str(item.get("region") or "").strip().casefold()
        if name in ("москва", "г москва", "г. москва"):
            return "77", "Москва"
        if name in ("московская область", "московская обл", "московская обл."):
            return "50", "Московская область"
        if name:
            return None, name  # An explicit different region must not become Moscow.
    for address in addresses:
        raw = str(address.get("raw") or "").casefold()
        if re.search(r"\bмосковская\s+обл", raw):
            return "50", "Московская область"
        if re.search(r"(?:^|[,;]\s*|\d{6}\s+)г\.?\s*москва\b", raw):
            return "77", "Москва"
    return None, None


def parse_okvedo_company_payload(data: dict[str, Any]) -> CompanyPayload:
    inn = str(data.get("inn") or "").strip()
    if not re.fullmatch(r"\d{10}", inn):
        raise ValueError("Okvedo: карточка не содержит ИНН юридического лица.")
    primary_code = normalize_okved(data.get("primary_okved"))
    additional: dict[str, OkvedItem] = {}
    for item in data.get("okveds") or []:
        if not isinstance(item, dict):
            continue
        code = normalize_okved(item.get("code"))
        if item.get("is_primary"):
            primary_code = primary_code or code
        elif code and code != primary_code:
            additional[code] = OkvedItem(code, item.get("title"))
    region_code, region_name = _region(data)
    emails, phones = set(), set()
    for item in data.get("emails") or []:
        value = item.get("email") if isinstance(item, dict) else item
        if isinstance(value, str) and (email := normalize_email(value)):
            emails.add(email)
    for item in data.get("phones") or []:
        value = item.get("number") if isinstance(item, dict) else item
        # Okvedo explicitly flags some aggregated numbers as junk or other countries.
        if isinstance(item, dict) and isinstance(item.get("dial_score"), (int, float)) and item["dial_score"] < 30:
            continue
        if isinstance(value, str) and (phone := normalize_phone(value)):
            phones.add(phone)
    return CompanyPayload(
        name=str(data.get("name_short") or data.get("name_full") or "Без наименования"),
        inn=inn, ogrn=str(data.get("ogrn") or "").strip() or None,
        primary_okved=OkvedItem(primary_code) if primary_code else None,
        additional_okveds=list(additional.values()), emails=sorted(emails), phone_numbers=sorted(phones),
        is_active=data.get("status") == "active", region_code=region_code, region_name=region_name,
    )


class OkvedoClient:
    fixed_page_size = None

    @staticmethod
    def expand_okved_codes(codes: list[str]) -> list[str]:
        expanded = []
        for code in codes:
            expanded.append(code)
            if code == "01":
                expanded.extend(AGRICULTURE_GROUP_CODES)
        return list(dict.fromkeys(expanded))

    def __init__(self, api_key: str, base_url: str = "https://okvedo.ru/api/v1", timeout_seconds: float = 30.0,
                 *, transport: httpx.BaseTransport | None = None):
        if not api_key.strip():
            raise ValueError("Okvedo API key is required")
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_seconds,
                                   headers={"X-Api-Key": api_key.strip(), "Accept": "application/json"}, transport=transport)
        # Keep only the current page's explicit provider data, keyed by INN.
        self._search_regions: dict[str, tuple[str | None, str | None]] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_: object):
        self.client.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self.client.get(path, params=params)
        except httpx.RequestError:
            raise DiscoveryAPIError("Okvedo временно недоступен. Повторите поиск позже.",
                                    stop_discovery=True, reason="connection_error") from None
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if response.status_code == 429:
            # A minute throttle is NOT evidence that the daily quota ran out.
            detail = str(payload.get("detail") or "").casefold()
            daily = bool(re.search(r"сут(?:ки|ок|очн)|дневн|\bday\b|\bdaily\b", detail))
            retry_after = response.headers.get("Retry-After", "")
            raise DiscoveryAPIError(
                "Суточный лимит Okvedo исчерпан." if daily else "Okvedo ограничил частоту запросов. Повторите поиск позже.",
                stop_discovery=True, reason="daily_limit" if daily else "rate_limit",
                retry_after_seconds=float(retry_after) if retry_after.isdigit() else None,
            )
        if response.status_code in (401, 403):
            raise DiscoveryAPIError("Okvedo отклонил доступ. Проверьте ключ и тариф.",
                                    stop_discovery=True, reason="access_denied")
        if response.is_error:
            raise DiscoveryAPIError(f"Okvedo вернул ошибку HTTP {response.status_code}.",
                                    stop_discovery=True,
                                    reason="service_unavailable" if response.is_server_error else "http_error")
        if "data" not in payload or payload.get("errors"):
            raise DiscoveryAPIError("Okvedo вернул ответ без корректных данных.",
                                    stop_discovery=True, reason="invalid_response")
        return payload

    def search_by_okved(self, code: str, *, region_code: str, limit: int = 10, page: int = 1) -> SearchPage:
        self._search_regions.clear()
        if region_code not in REGION_NAMES:
            raise ValueError("Okvedo: неподдерживаемый регион поиска.")
        payload = self._get("/companies", {"okved": code, "region": REGION_NAMES[region_code],
                                            "status": "active", "limit": min(max(limit, 1), 100), "page": page})
        if not isinstance(payload["data"], list):
            raise DiscoveryAPIError("Okvedo: неподдерживаемый формат поиска.", stop_discovery=True, reason="invalid_response")
        records = []
        for item in payload["data"]:
            if isinstance(item, dict) and re.fullmatch(r"\d{10}", str(item.get("inn") or "")):
                actual_region, _ = _region(item)
                inn = str(item["inn"])
                self._search_regions[inn] = _region(item)
                records.append({"ИНН": inn, "РегионКод": actual_region or ""})
        meta = payload.get("meta") or {}
        return SearchPage(
            records,
            max(int(meta.get("page") or page), 1),
            max(int(meta.get("pages") or page), 1),
            results_limited=meta.get("total_capped") is True,
        )

    def get_company(self, inn: str) -> CompanyPayload:
        if not re.fullmatch(r"\d{10}", inn):
            raise ValueError("Okvedo: некорректный ИНН.")
        data = self._get(f"/companies/{inn}")["data"]
        if not isinstance(data, dict) or str(data.get("inn")) != inn:
            raise DiscoveryAPIError("Okvedo вернул карточку с другим ИНН.")
        payload = parse_okvedo_company_payload(data)
        # Some cards omit all location fields even though /companies explicitly
        # supplies a region for that same INN. Use that evidence only for empty
        # cards; any card address/region/city takes precedence, even if unknown.
        if not any(data.get(field) for field in ("region", "region_code", "city", "addresses")):
            payload.region_code, payload.region_name = self._search_regions.get(inn, (None, None))
        return payload
