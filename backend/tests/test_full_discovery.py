"""Regression coverage for one-click exhaustive search, using no external APIs."""
from fastapi import BackgroundTasks
import httpx
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
import pytest

from app.config import Settings
from app.main import latest_search_run, start_search, stop_search_run
from app.models import Company, DiscoveryCursor, ExcludedCompany, SearchRun
from app.schemas import SearchRunCreate
from app.services.discovery import discover_new_companies, run_discovery
from app.services.okvedo import OkvedoClient
from app.serializers import search_run_to_dict
from app.services.provider import CompanyPayload, DiscoveryAPIError, OkvedItem, SearchPage


class Pages:
    fixed_page_size = None

    def __init__(self, count=350):
        self.inns = [str(7701000000 + i) for i in range(count)]
        self.searches = []
        self.cards = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def search_by_okved(self, code, *, region_code, limit, page):
        self.searches.append((region_code, page, limit))
        if region_code == "50":
            return SearchPage([], page, page)
        return SearchPage([{"ИНН": inn} for inn in self.inns[(page-1)*limit:page*limit]],
                          page, max(1, (len(self.inns)+limit-1)//limit))

    def get_company(self, inn):
        self.cards.append(inn)
        return CompanyPayload("Тест", inn, None, OkvedItem("49.41"), region_code="77")


@pytest.fixture(autouse=True)
def skip_network_pacing(monkeypatch):
    monkeypatch.setattr("app.services.discovery._wait_for_provider", lambda *a: None)


def full_run(db):
    run = SearchRun(status="pending", search_scope="full", requested_okved_codes=["49.41"])
    db.add(run)
    db.commit()
    return run


def use_session(db, monkeypatch):
    monkeypatch.setattr("app.services.discovery.SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False))


def test_full_search_collects_350_companies_beyond_two_pages_and_ten_candidates(db):
    run = full_run(db)
    client = Pages()
    discover_new_companies(db, run, client, 10)
    assert run.companies_created == 350
    assert client.searches == [("77", 1, 100), ("50", 1, 100)] + [("77", page, 100) for page in range(2, 5)]
    assert run.search_requests == 5
    assert run.company_requests == 350
    assert len(set(client.cards)) == 350


def test_full_search_passes_known_pages_and_exclusions_without_fetching_cards(db):
    run = full_run(db)
    client = Pages(305)
    db.add_all([ExcludedCompany(inn=inn, name="Удалена") for inn in client.inns[:300]])
    db.commit()
    discover_new_companies(db, run, client, 1)
    assert client.cards == client.inns[300:]
    assert run.companies_created == 5


def test_okvedo_passes_existing_pages_adds_empty_region_cards_and_skips_them_next_run(db):
    inns = [str(7701000000 + i) for i in range(210)]
    db.add_all([Company(inn=inn, name="Существующая") for inn in inns[:205]])
    db.add(ExcludedCompany(inn=inns[205], name="Исключена"))
    db.commit()
    cards = []
    pages = []

    def handler(request):
        if request.url.path.endswith("/companies"):
            page = int(request.url.params["page"])
            pages.append(page)
            records = [{"inn": inn, "region": "Москва"} for inn in inns[(page - 1) * 100:page * 100]]
            if request.url.params["region"] != "Москва":
                records = []
            return httpx.Response(200, json={"data": records, "meta": {"page": page, "pages": 3}})
        inn = request.url.path.rsplit("/", 1)[-1]
        cards.append(inn)
        return httpx.Response(200, json={"data": {
            "inn": inn, "name_short": "Новая", "status": "active", "region": None, "addresses": [],
        }})

    with OkvedoClient("test", transport=httpx.MockTransport(handler)) as client:
        first = full_run(db)
        discover_new_companies(db, first, client, 1, provider="okvedo")
        assert first.companies_created == 4
        assert first.skipped_known == 206
        assert first.skipped_unknown_region == 0
        assert pages == [1, 1, 2, 3]
        assert cards == inns[206:]
        second = full_run(db)
        discover_new_companies(db, second, client, 1, provider="okvedo")
    assert second.skipped_known == 210
    assert second.company_requests == second.companies_created == second.companies_updated == 0
    assert cards == inns[206:]
    assert db.scalar(select(func.count(Company.id))) == 209
    assert db.scalar(select(func.count(Company.id)).where(Company.name == "Существующая")) == 205


def test_full_search_reports_reasons_for_skipping_candidates(db):
    client = Pages(5)
    original = client.get_company
    db.add(Company(inn=client.inns[0], name="В базе"))
    db.commit()

    def get_company(inn):
        card = original(inn)
        if inn == client.inns[1]:
            card.is_active = False
        if inn == client.inns[2]:
            card.region_code = "71"
        if inn == client.inns[3]:
            card.region_code = None
        return card

    client.get_company = get_company
    run = full_run(db)
    discover_new_companies(db, run, client, 1)
    result = search_run_to_dict(run)
    assert result["companies_created"] == 1
    assert result["candidates_found"] == result["company_requests"] == 4
    for key in ("skipped_known", "skipped_inactive", "skipped_region", "skipped_unknown_region"):
        assert result[key] == 1


def test_full_fns_ignores_legacy_five_request_batch_caps(db, monkeypatch):
    use_session(db, monkeypatch)
    client = Pages(650)
    monkeypatch.setattr("app.services.discovery.ApiFnsClient", lambda *a, **k: client)
    run = full_run(db)
    run_discovery(run.id, Settings(_env_file=None, discovery_provider="api_fns", api_fns_key="test"), 1)
    db.refresh(run)
    assert run.status == "completed"
    assert run.companies_created == 650
    assert run.search_requests == 8
    assert run.provider_results == {"api_fns": "results_exhausted"}


def test_quota_stops_full_run_and_resume_keeps_exact_unprocessed_record(db, monkeypatch):
    use_session(db, monkeypatch)
    client = Pages(125)
    original = client.get_company

    def get_company(inn):
        if len(client.cards) == 115:
            raise DiscoveryAPIError("Лимит исчерпан", stop_discovery=True, reason="daily_limit")
        return original(inn)

    client.get_company = get_company
    monkeypatch.setattr("app.services.discovery.OkvedoClient", lambda *a, **k: client)
    settings = Settings(_env_file=None, discovery_provider="okvedo", okvedo_api_key="test")
    run = full_run(db)
    run_discovery(run.id, settings, 10)
    db.refresh(run)
    assert run.companies_created == 115
    assert run.provider_results == {"okvedo": "daily_limit"}
    cursor = db.scalar(select(DiscoveryCursor))
    assert (cursor.next_page, cursor.next_record_index) == (2, 15)
    client.get_company = original
    second = full_run(db)
    run_discovery(second.id, settings, 10)
    db.refresh(second)
    assert second.companies_created == 10
    assert db.scalar(select(func.count(Company.id))) == 125
    assert len(set(client.cards)) == len(client.cards) == 125


def test_stop_during_request_saves_response_and_does_not_call_next_provider(db, monkeypatch):
    use_session(db, monkeypatch)
    client = Pages(20)
    run = full_run(db)
    original = client.get_company

    def get_company(inn):
        result = original(inn)
        with sessionmaker(bind=db.get_bind())() as other:
            stop_search_run(run.id, other)
        return result

    client.get_company = get_company
    monkeypatch.setattr("app.services.discovery.CheckoClient", lambda *a, **k: client)
    monkeypatch.setattr("app.services.discovery.OkvedoClient", lambda *a, **k: pytest.fail("Next provider called after stop"))
    run_discovery(run.id, Settings(_env_file=None, discovery_provider="combined", checko_api_key="test", okvedo_api_key="test"), 10)
    db.refresh(run)
    assert run.status == "cancelled"
    assert run.companies_created == 1
    assert len(client.cards) == 1
    cursor = db.scalar(select(DiscoveryCursor))
    assert cursor.next_record_index == 1


def test_pending_stop_makes_no_provider_requests(db, monkeypatch):
    use_session(db, monkeypatch)
    run = full_run(db)
    stop_search_run(run.id, db)
    monkeypatch.setattr("app.services.discovery.OkvedoClient", lambda *a, **k: pytest.fail("Provider called"))
    run_discovery(run.id, Settings(_env_file=None, discovery_provider="combined", okvedo_api_key="test"), 10)
    db.refresh(run)
    assert run.status == "cancelled"


def test_rate_limit_retries_same_card_and_continues_automatically(db):
    client = Pages(25)
    run = full_run(db)
    original = client.get_company
    calls = []

    def get_company(inn):
        calls.append(inn)
        if len(calls) == 3:
            raise DiscoveryAPIError("Минутный лимит", stop_discovery=True, reason="rate_limit", retry_after_seconds=2)
        return original(inn)

    client.get_company = get_company
    discover_new_companies(db, run, client, 1)
    assert run.companies_created == 25
    assert run.company_requests == 26
    assert calls[2] == calls[3]
    assert run.errors_count == 0


def test_repeated_page_is_bounded(db):
    client = Pages(1)
    run = full_run(db)
    client.search_by_okved = lambda *a, **k: SearchPage([{"ИНН": client.inns[0]}], 1, 5)
    with pytest.raises(DiscoveryAPIError) as caught:
        discover_new_companies(db, run, client, 1)
    assert len(client.cards) == 1
    assert caught.value.reason == "pagination_stalled"


def test_repeated_click_reuses_active_run_and_latest_restores_it(db):
    tasks = BackgroundTasks()
    settings = Settings(_env_file=None, discovery_provider="demo")
    first = start_search(SearchRunCreate(), tasks, db, settings)
    second = start_search(SearchRunCreate(), tasks, db, settings)
    assert first["id"] == second["id"]
    assert first["search_scope"] == "full"
    assert len(tasks.tasks) == 1
    assert latest_search_run(db)["id"] == first["id"]
    stopped = stop_search_run(first["id"], db)
    assert stopped["cancel_requested"] is True
    assert db.scalar(select(func.count(SearchRun.id))) == 1


def test_search_schema_upgrade_keeps_old_runs_and_is_idempotent(monkeypatch):
    from app import database
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE search_runs (id INTEGER PRIMARY KEY, status VARCHAR(20))"))
        conn.execute(text("INSERT INTO search_runs VALUES (7, 'completed')"))
    monkeypatch.setattr(database, "engine", engine)
    database._upgrade_search_run_schema()
    database._upgrade_search_run_schema()
    with engine.connect() as conn:
        row = conn.execute(text("SELECT id, status, search_scope, cancel_requested, search_requests, skipped_known, skipped_region, skipped_unknown_region FROM search_runs")).one()
    assert tuple(row) == (7, "completed", "batch", 0, 0, 0, 0, 0)


def test_missing_card_does_not_block_remaining_candidates(db):
    client = Pages(20)
    run = full_run(db)
    original = client.get_company

    def get_company(inn):
        if inn == client.inns[0]:
            raise DiscoveryAPIError("Карточка не найдена", reason="not_found")
        return original(inn)

    client.get_company = get_company
    discover_new_companies(db, run, client, 2)
    assert run.companies_created == 19
    assert run.errors_count == 1
    assert client.cards == client.inns[1:]


def test_full_search_visits_every_query_before_second_page(db):
    run = full_run(db)
    run.requested_okved_codes = ['42.11', '77.32']
    db.commit()
    client = Pages()
    searches = []

    def search(code, *, region_code, limit, page):
        searches.append((code, region_code, page))
        inn = str(7701000000 + ['42.11', '77.32'].index(code) * 100 + int(region_code) + page)
        return SearchPage([{'ИНН': inn}], page, 2)

    client.search_by_okved = search
    discover_new_companies(db, run, client, 1)
    queries = [('42.11', '77'), ('42.11', '50'), ('77.32', '77'), ('77.32', '50')]
    assert searches == [(code, region, page) for page in (1, 2) for code, region in queries]
    assert run.companies_created == 8


def test_full_search_prioritizes_unvisited_then_oldest_queries(db):
    from datetime import datetime, timezone
    run = full_run(db)
    run.requested_okved_codes = ['42.11', '77.32']
    for code, region, day in [('42.11', '77', 14), ('42.11', '50', 13), ('77.32', '77', 4)]:
        db.add(DiscoveryCursor(provider='checko', okved_code=code, region_code=region,
                               page_size=100, updated_at=datetime(2026, 9, day, tzinfo=timezone.utc)))
    db.commit()
    searches = []
    client = Pages(0)

    def empty_search(code, *, region_code, limit, page):
        searches.append((code, region_code))
        return SearchPage([], page, page)

    client.search_by_okved = empty_search
    discover_new_companies(db, run, client, 1)
    assert searches == [('77.32', '50'), ('77.32', '77'), ('42.11', '50'), ('42.11', '77')]
    assert all(cursor.updated_at.year >= 2026 for cursor in db.scalars(select(DiscoveryCursor)))


@pytest.mark.parametrize('reason', ['connection_error', 'timeout', 'service_unavailable'])
@pytest.mark.parametrize('operation', ['search_by_okved', 'get_company'])
def test_transient_failure_retries_same_request_without_losing_companies(db, reason, operation):
    client = Pages(3)
    original = getattr(client, operation)
    attempts = []

    def flaky(*args, **kwargs):
        attempts.append((args, kwargs))
        if len(attempts) <= 2:
            raise DiscoveryAPIError('Temporary failure', reason=reason, stop_discovery=True)
        return original(*args, **kwargs)

    setattr(client, operation, flaky)
    run = full_run(db)
    discover_new_companies(db, run, client, 1)
    assert attempts[0] == attempts[1] == attempts[2]
    assert run.companies_created == 3
    assert run.errors_count == 0
    assert run.search_requests + run.company_requests == 7


def test_failed_card_preserves_position_and_other_query_can_add_companies(db):
    client = Pages(3)
    original_search = client.search_by_okved
    original_card = client.get_company
    other_inn = '5001000001'

    def search(code, *, region_code, limit, page):
        if region_code == '50':
            return SearchPage([{'ИНН': other_inn}], page, page)
        return original_search(code, region_code=region_code, limit=limit, page=page)

    def card(inn):
        if inn == client.inns[1]:
            raise DiscoveryAPIError('Timeout', reason='timeout', stop_discovery=True)
        return original_card(inn)

    client.search_by_okved = search
    client.get_company = card
    run = full_run(db)
    discover_new_companies(db, run, client, 1, propagate_stop_errors=True)
    cursor = db.scalar(select(DiscoveryCursor).where(DiscoveryCursor.region_code == '77'))
    assert (cursor.next_page, cursor.next_record_index) == (1, 1)
    assert set(db.scalars(select(Company.inn))) == {client.inns[0], other_inn}
    assert run.errors_count == 1
    client.get_company = original_card
    second = full_run(db)
    discover_new_companies(db, second, client, 1)
    assert second.companies_created == 2
    assert db.scalar(select(func.count(Company.id))) == 4


def test_unavailable_provider_is_bounded_and_next_run_starts_other_queries(db):
    client = Pages()
    calls = []

    def unavailable(code, *, region_code, limit, page):
        calls.append((code, region_code, page))
        raise DiscoveryAPIError('Offline', reason='connection_error', stop_discovery=True)

    client.search_by_okved = unavailable
    run = full_run(db)
    run.requested_okved_codes = ['42.11', '49.41', '77.32']
    db.commit()
    with pytest.raises(DiscoveryAPIError):
        discover_new_companies(db, run, client, 1, propagate_stop_errors=True)
    assert len(calls) == 12  # Four attempts on each of three queries, then stop.
    assert len(set(calls)) == 3
    second = full_run(db)
    second.requested_okved_codes = run.requested_okved_codes
    db.commit()
    with pytest.raises(DiscoveryAPIError):
        discover_new_companies(db, second, client, 1, propagate_stop_errors=True)
    assert calls[12] == ('49.41', '50', 1)
    assert len(set(calls)) == 6


def test_cancel_during_transient_retry_does_not_make_another_request(db, monkeypatch):
    from app.services.discovery import SearchCancelled
    run = full_run(db)
    client = Pages()
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(1)
        raise DiscoveryAPIError('Offline', reason='connection_error', stop_discovery=True)

    def wait(db, run, seconds):
        if seconds == 2:
            run.cancel_requested = True
            db.commit()

    client.search_by_okved = unavailable
    monkeypatch.setattr('app.services.discovery._wait_for_provider', wait)
    with pytest.raises(SearchCancelled):
        discover_new_companies(db, run, client, 1)
    assert len(calls) == 1
