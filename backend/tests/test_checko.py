from app.services.checko import normalize_email, parse_company_payload


def test_parse_company_payload_reads_checko_russian_keys():
    payload = parse_company_payload(
        {
            "ИНН": "7701234567",
            "ОГРН": "1267700000000",
            "НаимСокр": 'ООО "ТЕСТ"',
            "Статус": {"Код": "001", "Наим": "Действует"},
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
