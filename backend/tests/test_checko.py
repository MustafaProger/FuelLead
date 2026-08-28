import httpx
import pytest

from app.services.checko import CheckoAPIError, CheckoClient, normalize_email, parse_company_payload


def test_parse_company_payload_reads_checko_russian_keys():
    payload = parse_company_payload(
        {
            "ИНН": "7701234567",
            "ОГРН": "1267700000000",
            "НаимСокр": 'ООО "ТЕСТ"',
            "Статус": {"Код": "001", "Наим": "Действует"},
            "Регион": {"Код": "77", "Наим": "Москва"},
            "ОКВЭД": {"Код": "49.41", "Наим": "Грузовой автотранспорт"},
            "ОКВЭДДоп": [{"Код": "77.32", "Наим": "Аренда строительных машин"}],
            "Контакты": {"Емэйл": [" INFO@Test.ru ", "info@test.ru", "bad address"]},
        }
    )

    assert payload.inn == "7701234567"
    assert payload.is_active is True
    assert payload.primary_okved.code == "49.41"
    assert payload.additional_okveds[0].code == "77.32"
    assert payload.emails == ["info@test.ru"]
    assert payload.region_code == "77"
    assert payload.region_name == "Москва"


def test_inactive_company_is_not_marked_active():
    payload = parse_company_payload(
        {"ИНН": "1", "НаимСокр": "Тест", "Статус": {"Наим": "Не действует"}}
    )
    assert payload.is_active is False


def test_parse_company_payload_deduplicates_additional_okveds():
    payload = parse_company_payload(
        {
            "ИНН": "7701234567",
            "НаимСокр": "Тест",
            "Статус": {"Наим": "Действует"},
            "ОКВЭДДоп": [
                {"Код": "42.11", "Наим": None},
                {"Код": "42.11", "Наим": "Строительство автомобильных дорог"},
                {"Код": "49.41", "Наим": "Грузовой автотранспорт"},
            ],
        }
    )

    assert [(item.code, item.name) for item in payload.additional_okveds] == [
        ("42.11", "Строительство автомобильных дорог"),
        ("49.41", "Грузовой автотранспорт"),
    ]


def test_email_normalization_rejects_invalid_values():
    assert normalize_email("MAILTO:Sales@Example.ru") == "sales@example.ru"
    assert normalize_email("no-at-sign") is None


def test_daily_limit_error_is_friendly_and_does_not_expose_key():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["key"] == "super-secret-key"
        return httpx.Response(
            403,
            json={
                "meta": {
                    "status": "error",
                    "today_request_count": 100,
                    "message": "Превышен суточный лимит запросов для бесплатного тарифа",
                    "balance": 0.0,
                }
            },
        )

    with CheckoClient(
        "super-secret-key",
        "https://api.checko.ru/v2",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(CheckoAPIError) as caught:
            client.search_by_okved("49.41", region_code="77", limit=2)

    assert caught.value.stop_discovery is True
    assert "Суточный лимит Checko исчерпан" in str(caught.value)
    assert "100 из 100" in str(caught.value)
    assert "super-secret-key" not in str(caught.value)


def test_client_switches_to_next_key_and_keeps_using_it():
    used_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        api_key = request.url.params["key"]
        used_keys.append(api_key)
        assert request.url.params["region"] == "50"
        if api_key == "exhausted-key":
            return httpx.Response(
                403,
                json={
                    "meta": {
                        "status": "error",
                        "today_request_count": 100,
                        "message": "Превышен суточный лимит запросов",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "meta": {"status": "ok"},
                "data": {"Записи": [{"ИНН": "5001000000", "РегионКод": "50"}]},
            },
        )

    with CheckoClient(
        ["exhausted-key", "backup-key"],
        "https://api.checko.ru/v2",
        transport=httpx.MockTransport(handler),
    ) as client:
        first = client.search_by_okved("49.41", region_code="50", limit=1)
        second = client.search_by_okved("42.11", region_code="50", limit=1)

    assert first.records[0]["ИНН"] == "5001000000"
    assert second.records[0]["ИНН"] == "5001000000"
    assert used_keys == ["exhausted-key", "backup-key", "backup-key"]


def test_search_returns_pagination_metadata_and_forwards_page():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["limit"] == "2"
        assert request.url.params["page"] == "3"
        return httpx.Response(
            200,
            json={
                "meta": {"status": "ok"},
                "data": {
                    "СтрВсего": 8,
                    "СтрТекущ": 3,
                    "Записи": [{"ИНН": "7701000001"}],
                },
            },
        )

    with CheckoClient(
        "key",
        "https://api.checko.ru/v2",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client.search_by_okved("49.41", region_code="77", limit=2, page=3)

    assert result.current_page == 3
    assert result.total_pages == 8
    assert result.records == [{"ИНН": "7701000001"}]


def test_search_requires_two_digit_region_code():
    with CheckoClient(
        "key",
        "https://api.checko.ru/v2",
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    ) as client:
        with pytest.raises(ValueError, match="two digits"):
            client.search_by_okved("49.41", region_code="all")
