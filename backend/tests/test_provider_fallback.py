from itertools import product

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.models import Company, DiscoveryCursor, SearchRun
from app.services.discovery import run_discovery
from app.services.provider import CompanyPayload, DiscoveryAPIError, OkvedItem, SearchPage


@pytest.mark.parametrize("states", list(product(["ok", "daily_limit", "timeout"], repeat=3)))
def test_fns_runs_last_after_every_primary_outcome(db, monkeypatch, states):
    run = SearchRun(status="pending", requested_okved_codes=["49.41"])
    db.add(run)
    db.commit()
    calls = []
    reasons = dict(zip(("checko", "okvedo", "dadata"), states))

    def stage(db, run, settings, limit, provider):
        calls.append(provider)
        if provider in reasons and reasons[provider] != "ok":
            raise DiscoveryAPIError("Provider stopped", stop_discovery=True, reason=reasons[provider])
        return False

    monkeypatch.setattr("app.services.discovery._run_provider_discovery", stage)
    monkeypatch.setattr("app.services.discovery.SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    settings = Settings(_env_file=None, discovery_provider="combined", checko_api_key="c", okvedo_api_key="o", dadata_api_key="d", api_fns_key="f")
    run_discovery(run.id, settings, 1)
    assert calls == ["checko", "okvedo", "dadata", "api_fns"]


@pytest.mark.parametrize("reason", ["rate_limit", "access_denied", "invalid_key", "invalid_response", "connection_error", "keys_unavailable"])
def test_unknown_or_temporary_primary_failure_allows_fns(db, monkeypatch, reason):
    run = SearchRun(status="pending", requested_okved_codes=["49.41"])
    db.add(run)
    db.commit()
    calls = []

    def stage(db, run, settings, limit, provider):
        calls.append(provider)
        if provider != "api_fns":
            raise DiscoveryAPIError("Stopped", stop_discovery=True, reason=reason)
        return False

    monkeypatch.setattr("app.services.discovery._run_provider_discovery", stage)
    monkeypatch.setattr("app.services.discovery.SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    run_discovery(run.id, Settings(_env_file=None, discovery_provider="combined", checko_api_key="c", api_fns_key="f"), 1)
    db.refresh(run)
    assert calls == ["checko", "api_fns"]
    assert run.provider_results == {"checko": reason, "api_fns": "results_exhausted"}
    assert run.errors_count == 1


def test_partial_results_survive_exhaustion_and_all_four_stages_run_in_order(db, monkeypatch):
    events = []
    providers = ("checko", "okvedo", "dadata", "api_fns")
    inns = {p: str(7701000000 + i) for i, p in enumerate(providers)}

    class Client:
        fixed_page_size = 10

        def __init__(self, provider):
            self.provider = provider

        def __enter__(self):
            events.append(self.provider)
            return self

        def __exit__(self, *_):
            pass

        def search_by_okved(self, code, *, region_code, limit, page):
            if region_code == "50" and self.provider != "api_fns":
                raise DiscoveryAPIError("Quota exhausted", stop_discovery=True, reason="daily_limit")
            return SearchPage([{"ИНН": inns[self.provider], "РегионКод": "77"}], page, page)

        def get_company(self, inn):
            return CompanyPayload(self.provider, inn, None, OkvedItem("49.41"), region_code="77")

    for attribute, provider in zip(("CheckoClient", "OkvedoClient", "DaDataClient", "ApiFnsClient"), providers):
        monkeypatch.setattr(f"app.services.discovery.{attribute}", lambda *a, p=provider, **k: Client(p))
    monkeypatch.setattr("app.services.discovery.SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    run = SearchRun(status="pending", requested_okved_codes=["49.41"])
    db.add(run)
    db.commit()
    run_discovery(run.id, Settings(_env_file=None, discovery_provider="combined", checko_api_key="c", okvedo_api_key="o", dadata_api_key="d", api_fns_key="f"), 1)
    db.expire_all()
    stored = db.get(SearchRun, run.id)
    assert events == list(providers)
    assert stored.status == "completed"
    assert stored.companies_created == 4
    assert stored.errors_count == 3
    assert set(db.scalars(select(Company.provider))) == set(providers)
    assert set(db.scalars(select(DiscoveryCursor.provider))) == set(providers)


def test_primary_deferral_allows_fns_without_losing_partial_reason(db, monkeypatch):
    calls = []

    def stage(db, run, settings, limit, provider):
        calls.append(provider)
        if provider == "okvedo":
            run.provider_results = {provider: "known_results_deferred"}
        return False

    monkeypatch.setattr("app.services.discovery._run_provider_discovery", stage)
    monkeypatch.setattr("app.services.discovery.SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    run = SearchRun(status="pending", requested_okved_codes=["49.41"])
    db.add(run)
    db.commit()
    run_discovery(run.id, Settings(_env_file=None, discovery_provider="combined", okvedo_api_key="o", api_fns_key="f"), 1)
    db.refresh(run)
    assert calls == ["okvedo", "api_fns"]
    assert run.provider_results == {"okvedo": "known_results_deferred", "api_fns": "results_exhausted"}
    assert "частично" in run.progress_message
    assert "Доступная выдача пройдена" not in run.progress_message


@pytest.mark.parametrize("primary_state", ["empty", "keys_unavailable", "rate_limit", "timeout"])
def test_empty_or_failed_primaries_still_allow_fns_to_add_a_company(db, monkeypatch, primary_state):
    calls = []

    class Client:
        fixed_page_size = 100

        def __init__(self, provider):
            self.provider = provider

        def __enter__(self):
            calls.append(self.provider)
            return self

        def __exit__(self, *_):
            pass

        def search_by_okved(self, code, *, region_code, limit, page):
            if self.provider == "checko" and primary_state != "empty":
                raise DiscoveryAPIError("Source unavailable", stop_discovery=True, reason=primary_state)
            records = [{"ИНН": "7701000001"}] if self.provider == "api_fns" and region_code == "77" else []
            return SearchPage(records, page, page)

        def get_company(self, inn):
            return CompanyPayload("Новая", inn, None, OkvedItem("49.41"), region_code="77")

    for attribute, provider in (("CheckoClient", "checko"), ("OkvedoClient", "okvedo"), ("ApiFnsClient", "api_fns")):
        monkeypatch.setattr(f"app.services.discovery.{attribute}", lambda *a, p=provider, **k: Client(p))
    monkeypatch.setattr("app.services.discovery._wait_for_provider", lambda *a: None)
    monkeypatch.setattr("app.services.discovery.SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    run = SearchRun(status="pending", search_scope="full", requested_okved_codes=["49.41"])
    db.add(run)
    db.commit()
    run_discovery(run.id, Settings(_env_file=None, discovery_provider="combined", checko_api_key="c", okvedo_api_key="o", api_fns_key="f"), 1)
    db.refresh(run)
    assert calls == ["checko", "okvedo", "api_fns"]
    assert run.companies_created == 1
    assert run.company_requests == 1
    # Primary attempts, including retries, must not consume the FNS stage budget.
    assert run.search_requests == {"empty": 6, "keys_unavailable": 5, "rate_limit": 8, "timeout": 12}[primary_state]
    assert run.provider_results["api_fns"] == "results_exhausted"
    assert run.errors_count == (0 if primary_state == "empty" else 2 if primary_state == "timeout" else 1)


def test_company_quota_preserves_retry_cursor_and_partial_success(db, monkeypatch):
    class Client:
        fixed_page_size = 2

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def search_by_okved(self, code, *, region_code, limit, page):
            return SearchPage([{"ИНН": "7701000001"}, {"ИНН": "7701000002"}], 1, 1)

        def get_company(self, inn):
            if inn == "7701000002":
                raise DiscoveryAPIError("Company quota exhausted", stop_discovery=True, reason="daily_limit")
            return CompanyPayload("Тест", inn, None, OkvedItem("49.41"), region_code="77")

    monkeypatch.setattr("app.services.discovery.OkvedoClient", lambda *a, **kw: Client())
    monkeypatch.setattr("app.services.discovery.SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    run = SearchRun(status="pending", requested_okved_codes=["49.41"])
    db.add(run)
    db.commit()
    run_discovery(run.id, Settings(_env_file=None, discovery_provider="combined", okvedo_api_key="o"), 2)
    db.expire_all()
    stored = db.get(SearchRun, run.id)
    cursor = db.scalar(select(DiscoveryCursor).where(DiscoveryCursor.provider == "okvedo"))
    assert stored.status == "completed" and stored.companies_created == 1
    assert stored.errors_count == 1
    assert (cursor.next_page, cursor.next_record_index) == (1, 1)
