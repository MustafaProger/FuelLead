"""Regression coverage for one-click exhaustive search, using no external APIs."""
from fastapi import BackgroundTasks
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
import pytest

from app.config import Settings
from app.main import latest_search_run, start_search, stop_search_run
from app.models import Company, DiscoveryCursor, ExcludedCompany, SearchRun
from app.schemas import SearchRunCreate
from app.services.discovery import discover_new_companies, run_discovery
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
    assert client.searches == [("77", page, 100) for page in range(1, 5)] + [("50", 1, 100)]
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
        row = conn.execute(text("SELECT id, status, search_scope, cancel_requested, search_requests FROM search_runs")).one()
    assert tuple(row) == (7, "completed", "batch", 0, 0)


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
