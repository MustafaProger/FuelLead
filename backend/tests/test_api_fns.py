import httpx
import pytest

from app.services.api_fns import (
    ApiFnsAPIError,
    ApiFnsClient,
    parse_api_fns_company_payload,
)


def company_data() -> dict:
    return {
        "ИНН": "7701234567",
        "ОГРН": "1267700000000",
        "НаимСокрЮЛ": 'ООО "ТЕСТ ТРАНС"',
        "НаимПолнЮЛ": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ТЕСТ ТРАНС"',
        "Статус": "Действующее",
        "Адрес": {
            "КодРегион": "77",
            "АдресПолн": "г. Москва, ул. Тестовая, д. 1",
            "АдресДетали": {"Регион": {"Наим": "ГОРОД МОСКВА"}},
        },
        "ОснВидДеят": {"Код": "49.41", "Текст": "Деятельность автомобильного грузового транспорта"},
        "ДопВидДеят": [
            {"Код": "52.21.2", "Текст": "Вспомогательная деятельность, связанная с автотранспортом"},
            {"Код": "52.21.2", "Текст": "Дубликат"},
            {"Код": "77.32", "Текст": "Аренда строительных машин"},
        ],
        "Контакты": {
            "Телефон": ["+7 (495) 123-45-67", "8 495 123-45-67", "короткий 123"],
            "e-mail": [" INFO@Test.ru ", "info@test.ru", "bad address"],
        },
    }


def test_parser_reads_api_fns_company_card_and_deduplicates_contacts():
    payload = parse_api_fns_company_payload(company_data())

    assert payload.name == 'ООО "ТЕСТ ТРАНС"'
    assert payload.inn == "7701234567"
    assert payload.ogrn == "1267700000000"
    assert payload.is_active is True
    assert payload.region_code == "77"
    assert payload.region_name == "ГОРОД МОСКВА"
    assert (payload.primary_okved.code, payload.primary_okved.name) == (
        "49.41",
        "Деятельность автомобильного грузового транспорта",
    )
    assert [(item.code, item.name) for item in payload.additional_okveds] == [
        ("52.21.2", "Вспомогательная деятельность, связанная с автотранспортом"),
        ("77.32", "Аренда строительных машин"),
    ]
    assert payload.emails == ["info@test.ru"]
    assert payload.phone_numbers == ["+74951234567"]


def test_parser_marks_non_active_status_inactive_and_pads_region_code():
    data = company_data()
    data["Статус"] = "Ликвидировано"
    data["Адрес"]["КодРегион"] = 5

    payload = parse_api_fns_company_payload(data)

    assert payload.is_active is False
    assert payload.region_code == "05"


def test_search_uses_primary_okved_region_contact_filters_and_page():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/search"
        assert request.url.params["key"] == "secret-key"
        assert request.url.params["q"] == "any"
        assert request.url.params["page"] == "2"
        search_filter = request.url.params["filter"]
        assert search_filter == "active+onlyul+okved49.41+region77+withphone+withemail"
        assert "okvedgroup" not in search_filter
        return httpx.Response(
            200,
            json={
                "items": [
                    {"ЮЛ": {"ИНН": "7701234567", "ОГРН": "1267700000000"}},
                    {"ИП": {"ИНН": "770100000000"}},
                ],
                "filter_any_count": 205,
                "nextpage": True,
            },
        )

    with ApiFnsClient(
        "secret-key",
        transport=httpx.MockTransport(handler),
        require_phone=True,
        require_email=True,
    ) as client:
        page = client.search_by_okved("49.41", region_code="77", limit=2, page=2)

    assert page.current_page == 2
    assert page.total_pages == 3
    assert page.records == [{"ИНН": "7701234567", "ОГРН": "1267700000000", "РегионКод": ""}]


def test_search_uses_nextpage_when_total_count_is_absent():
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"items": [], "nextpage": True})
    )
    with ApiFnsClient("key", transport=transport) as client:
        page = client.search_by_okved("49.41", region_code="50", page=4)

    assert page.current_page == 4
    assert page.total_pages == 5


def test_egr_client_parses_legal_entity_card():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/egr"
        assert request.url.params["req"] == "7701234567"
        assert request.url.params["key"] == "secret-key"
        return httpx.Response(200, json={"items": [{"ЮЛ": company_data()}]})

    with ApiFnsClient("secret-key", transport=httpx.MockTransport(handler)) as client:
        payload = client.get_company("7701234567")

    assert payload.inn == "7701234567"
    assert payload.primary_okved.code == "49.41"
    assert payload.emails == ["info@test.ru"]
    assert payload.phone_numbers == ["+74951234567"]


@pytest.mark.parametrize(
    ("status_code", "provider_error", "reason", "message_part"),
    [
        (401, "API-ключ не действителен", "invalid_key", "отклонил ключ"),
        (200, "Превышен лимит запросов метода search", "quota_exhausted", "Лимит запросов"),
        (403, "Доступ разрешен только с указанного IP-адреса", "ip_restriction", "ограничения IP"),
    ],
)
def test_provider_errors_are_actionable_and_stop_discovery(
    status_code: int,
    provider_error: str,
    reason: str,
    message_part: str,
):
    transport = httpx.MockTransport(
        lambda _: httpx.Response(status_code, json={"error": provider_error})
    )
    with ApiFnsClient("secret-key", transport=transport) as client:
        with pytest.raises(ApiFnsAPIError) as caught:
            client.search_by_okved("49.41", region_code="77")

    assert caught.value.stop_discovery is True
    assert caught.value.reason == reason
    assert message_part in str(caught.value)
    assert "secret-key" not in str(caught.value)


def test_unknown_provider_error_redacts_key():
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"error": "Ошибка key=secret-key"})
    )
    with ApiFnsClient("secret-key", transport=transport) as client:
        with pytest.raises(ApiFnsAPIError) as caught:
            client.get_statistics()

    assert "secret-key" not in str(caught.value)
    assert "<redacted>" in str(caught.value)


def test_timeout_stops_discovery_without_exposing_request_url():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with ApiFnsClient("secret-key", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ApiFnsAPIError) as caught:
            client.search_by_okved("49.41", region_code="77")

    assert caught.value.stop_discovery is True
    assert caught.value.reason == "timeout"
    assert "secret-key" not in str(caught.value)
    assert "https://" not in str(caught.value)


def test_non_json_unauthorized_response_is_reported_as_invalid_key():
    transport = httpx.MockTransport(
        lambda _: httpx.Response(401, text="Unauthorized")
    )
    with ApiFnsClient("secret-key", transport=transport) as client:
        with pytest.raises(ApiFnsAPIError) as caught:
            client.get_statistics()

    assert caught.value.reason == "invalid_key"
    assert caught.value.stop_discovery is True
    assert "отклонил ключ" in str(caught.value)
