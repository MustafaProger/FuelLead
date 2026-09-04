from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json

import httpx
import pytest

from app.services.checko import CheckoClient
from app.services.dadata import DaDataClient, parse_dadata_company_payload
from app.services.okvedo import OkvedoClient, parse_okvedo_company_payload
from app.services.provider import DiscoveryAPIError


def dadata_card():
    return {"value": "ООО ТЕСТ", "data": {
        "inn": "7701000001", "ogrn": "1027700000001", "type": "LEGAL", "branch_type": "MAIN",
        "name": {"short_with_opf": "ООО ТЕСТ"}, "state": {"status": "ACTIVE"}, "okved": "49.41",
        "okveds": [{"code": "49.41", "main": True}, {"code": "42.11", "name": "Дороги", "main": False}],
        "address": {"data": {"region_kladr_id": "5000000000000", "region_with_type": "Московская обл"}},
        "phones": [{"value": "+7 (495) 123-45-67"}], "emails": [{"value": " INFO@example.ru "}],
    }}


def test_dadata_parses_main_company_contacts_and_current_address():
    payload = parse_dadata_company_payload(dadata_card())
    assert payload.inn == "7701000001"
    assert payload.region_code == "50"  # Current address, not the INN prefix.
    assert payload.is_active
    assert payload.primary_okved.code == "49.41"
    assert [o.code for o in payload.additional_okveds] == ["42.11"]
    assert payload.emails == ["info@example.ru"]
    assert payload.phone_numbers == ["+74951234567"]


def test_dadata_filters_and_fixed_batch_dont_fake_pagination():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.headers["Authorization"] == "Token secret"
        assert "x-secret" not in request.headers
        body = json.loads(request.content)
        if request.url.path.endswith("/suggest/party"):
            assert body["locations"] == [{"kladr_id": "5000000000000"}]
            assert body["okved"] == ["49.41"]
            assert body["type"] == "LEGAL" and body["status"] == ["ACTIVE"]
            assert body["count"] == 20
            assert "page" not in body
        else:
            assert body == {"query": "7701000001", "count": 1, "type": "LEGAL", "branch_type": "MAIN"}
        return httpx.Response(200, json={"suggestions": [dadata_card()]})

    with DaDataClient("secret", secret_key="private", transport=httpx.MockTransport(handler)) as client:
        page = client.search_by_okved("49.41", region_code="50", limit=1, page=9)
        card = client.get_company(page.records[0]["ИНН"])
    assert page.current_page == page.total_pages == 1
    assert card.region_code == "50"
    assert len(calls) == 2


@pytest.mark.parametrize("remaining,days_ago,reason", [(0, 0, "daily_limit"), (1, 0, "access_denied"), (None, 0, "access_denied"), (0, 1, "access_denied")])
def test_dadata_403_requires_fresh_stat_proof(remaining, days_ago, reason):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.host == "dadata.ru":
            assert request.headers["X-Secret"] == "private"
            date = (datetime.now(ZoneInfo("Europe/Moscow")) - timedelta(days=days_ago)).date().isoformat()
            return httpx.Response(200, json={"date": date, "remaining": {"suggestions": remaining}})
        return httpx.Response(403, json={"message": "Feature SUGGESTIONS disabled for token secret"})

    with DaDataClient("secret", secret_key="private", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DiscoveryAPIError) as caught:
            client.search_by_okved("49.41", region_code="77")
    assert caught.value.reason == reason
    assert "secret" not in str(caught.value) and "private" not in str(caught.value)
    assert calls[-1] == "/api/v2/stat/daily"


def test_dadata_unreachable_stats_does_not_claim_exhaustion():
    def handler(request):
        if request.url.host == "dadata.ru":
            raise httpx.ReadTimeout("secret", request=request)
        return httpx.Response(403)

    with DaDataClient("secret", secret_key="private", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DiscoveryAPIError) as caught:
            client.get_company("7701000001")
    assert caught.value.reason == "access_denied"


def test_okvedo_card_uses_address_when_top_level_region_missing():
    card = parse_okvedo_company_payload({
        "inn": "7701000001", "name_short": "Тест", "status": "active", "region": None,
        "primary_okved": "494100", "addresses": [{"raw": "119618, г. Москва, ул. Примерная, д. 4"}],
        "okveds": [{"code": "42.11", "title": "Дороги", "is_primary": False}],
        "phones": [{"number": "+74951234567", "dial_score": 79}, {"number": "+74951234568", "dial_score": 0}],
        "emails": [{"email": " INFO@EXAMPLE.RU "}],
    })
    assert card.region_code == "77"
    assert card.primary_okved.code == "49.41.00"
    assert card.additional_okveds[0].code == "42.11"
    assert card.phone_numbers == ["+74951234567"]
    assert card.emails == ["info@example.ru"]


@pytest.mark.parametrize("data,region", [
    ({"region": "Московская область"}, "50"),
    ({"addresses": [{"region_code": "50"}]}, "50"),
    ({"region": "Калужская область", "addresses": [{"raw": "г. Москва"}]}, None),
    ({"addresses": [{"raw": "Московская область, г. Мытищи, ул Московская"}]}, "50"),
    ({"addresses": [{"raw": "г. Тула, ул. Московская"}]}, None),
    ({}, None),
])
def test_okvedo_never_infers_region_from_inn(data, region):
    card = parse_okvedo_company_payload({"inn": "7701000001", **data})
    assert card.region_code == region


def test_okvedo_search_passes_filters_pagination_and_skips_non_legal_inns():
    def handler(request):
        assert request.headers["X-Api-Key"] == "secret"
        assert "secret" not in str(request.url)
        assert dict(request.url.params) == {"okved": "49.41", "region": "Московская область", "status": "active", "limit": "2", "page": "3"}
        return httpx.Response(200, json={"data": [{"inn": "5001000001", "region": "Московская область"}, {"inn": "500100000001"}, {"no_inn": True}], "meta": {"page": 3, "pages": 12}, "errors": []})

    with OkvedoClient("secret", transport=httpx.MockTransport(handler)) as client:
        page = client.search_by_okved("49.41", region_code="50", limit=2, page=3)
    assert page.records == [{"ИНН": "5001000001", "РегионКод": "50"}]
    assert (page.current_page, page.total_pages) == (3, 12)


@pytest.mark.parametrize("message,reason", [("Превышен предел 60 запросов в минуту", "rate_limit"), ("Превышен предел 5000 запросов в сутки", "daily_limit"), ("Daily request limit exceeded", "daily_limit"), ("Too many requests", "rate_limit")])
def test_okvedo_rate_limit_is_distinct_from_daily_quota(message, reason):
    with OkvedoClient("secret", transport=httpx.MockTransport(lambda _: httpx.Response(429, json={"detail": message}))) as client:
        with pytest.raises(DiscoveryAPIError) as caught:
            client.search_by_okved("49.41", region_code="77")
    assert caught.value.reason == reason


@pytest.mark.parametrize("client_type", [OkvedoClient, DaDataClient])
@pytest.mark.parametrize("status,payload,reason", [(401, {"detail": "secret"}, "access_denied"), (500, {"detail": "secret"}, "http_error"), (200, [], "invalid_response"), (200, {}, "invalid_response")])
def test_new_provider_errors_are_secret_safe(client_type, status, payload, reason):
    with client_type("secret", transport=httpx.MockTransport(lambda _: httpx.Response(status, json=payload))) as client:
        with pytest.raises(DiscoveryAPIError) as caught:
            client.search_by_okved("49.41", region_code="77")
    assert caught.value.reason == reason
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("all_exhausted", [True, False])
def test_checko_reports_daily_limit_only_if_every_key_is_exhausted(all_exhausted):
    def handler(request):
        exhausted = all_exhausted or request.url.params["key"] == "first"
        return httpx.Response(403, json={"meta": {"status": "error", "message": "Превышен суточный лимит" if exhausted else "Ключ недействителен"}})

    with CheckoClient(["first", "second"], "https://api.checko.ru/v2", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DiscoveryAPIError) as caught:
            client.search_by_okved("49.41", region_code="77")
    assert caught.value.reason == ("daily_limit" if all_exhausted else "keys_unavailable")


def test_checko_search_and_card_quotas_can_use_different_keys():
    calls = []

    def handler(request):
        key = request.url.params["key"]
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append((method, key))
        if (method, key) in [("search", "first"), ("company", "second")]:
            return httpx.Response(403, json={"meta": {"status": "error", "message": "Превышен суточный лимит"}})
        data = {"Записи": [{"ИНН": "7701000001"}]} if method == "search" else {"ИНН": "7701000001", "Статус": {"Наим": "Действует"}}
        return httpx.Response(200, json={"meta": {"status": "ok"}, "data": data})

    with CheckoClient(["first", "second"], "https://api.checko.ru/v2", transport=httpx.MockTransport(handler)) as client:
        client.search_by_okved("49.41", region_code="77")
        card = client.get_company("7701000001")
        client.search_by_okved("42.11", region_code="77")
        client.get_company("7701000001")
    assert card.inn == "7701000001"
    assert calls == [("search", "first"), ("search", "second"), ("company", "first"), ("search", "second"), ("company", "first")]
