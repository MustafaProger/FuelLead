from app.commands import check_api_fns
from app.config import Settings
from app.services.provider import CompanyPayload, OkvedItem, SearchPage


class FakeApiFnsClient:
    search_calls = 0
    egr_calls = 0
    stat_calls = 0

    def __init__(self, api_key, *_, **__):
        assert api_key == "secret-key"

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def get_statistics(self):
        self.__class__.stat_calls += 1
        offset = 0 if self.stat_calls == 1 else 1
        return {
            "Методы": {
                "search": {"Лимит": "100", "Истрачено": str(10 + offset)},
                "egr": {"Лимит": "100", "Истрачено": str(20 + offset)},
            }
        }

    def search_by_okved(self, code, *, region_code, page, limit):
        assert (code, region_code, page, limit) == ("49.41", "77", 1, 1)
        self.__class__.search_calls += 1
        return SearchPage(
            records=[{"ИНН": "7701234567"}],
            current_page=1,
            total_pages=2,
        )

    def get_company(self, inn):
        assert inn == "7701234567"
        self.__class__.egr_calls += 1
        return CompanyPayload(
            name='ООО "ТЕСТ"',
            inn=inn,
            ogrn="1267700000000",
            primary_okved=OkvedItem("49.41", "Грузовой транспорт"),
            additional_okveds=[OkvedItem("52.21.2")],
            emails=["info@example.ru"],
            phone_numbers=["+74951234567"],
            region_code="77",
            region_name="Москва",
        )


def test_safe_command_uses_exactly_one_search_and_egr(monkeypatch, capsys):
    FakeApiFnsClient.search_calls = 0
    FakeApiFnsClient.egr_calls = 0
    FakeApiFnsClient.stat_calls = 0
    settings = Settings(
        _env_file=None,
        discovery_provider="api_fns",
        api_fns_key="secret-key",
    )
    monkeypatch.setattr(check_api_fns, "ApiFnsClient", FakeApiFnsClient)
    monkeypatch.setattr(check_api_fns, "get_settings", lambda: settings)
    monkeypatch.setattr("sys.argv", ["check_api_fns"])

    check_api_fns.main()

    output = capsys.readouterr().out
    assert FakeApiFnsClient.stat_calls == 2
    assert FakeApiFnsClient.search_calls == 1
    assert FakeApiFnsClient.egr_calls == 1
    assert 'usage_delta={"search": 1, "egr": 1}' in output
    assert "secret-key" not in output


def test_stat_only_command_does_not_call_search_or_egr(monkeypatch, capsys):
    FakeApiFnsClient.search_calls = 0
    FakeApiFnsClient.egr_calls = 0
    FakeApiFnsClient.stat_calls = 0
    settings = Settings(
        _env_file=None,
        discovery_provider="api_fns",
        api_fns_key="secret-key",
    )
    monkeypatch.setattr(check_api_fns, "ApiFnsClient", FakeApiFnsClient)
    monkeypatch.setattr(check_api_fns, "get_settings", lambda: settings)
    monkeypatch.setattr("sys.argv", ["check_api_fns", "--stat-only"])

    check_api_fns.main()

    output = capsys.readouterr().out
    assert FakeApiFnsClient.stat_calls == 1
    assert FakeApiFnsClient.search_calls == 0
    assert FakeApiFnsClient.egr_calls == 0
    assert 'stat={"search": {"limit": "100", "spent": "10"}, "egr": {"limit": "100", "spent": "20"}}' in output
    assert "secret-key" not in output
