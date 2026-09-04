from itertools import product

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.models import Company, DiscoveryCursor, SearchRun
from app.services.discovery import run_discovery
from app.services.provider import CompanyPayload, DiscoveryAPIError, OkvedItem, SearchPage


@pytest.mark.parametrize("states", list(product(["ok", "daily_limit", "timeout"], repeat=3)))
def test_fns_requires_confirmed_exhaustion_of_every_primary(db, monkeypatch, states):
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
    assert calls == ["checko", "okvedo", "dadata"] + (["api_fns"] if states == ("daily_limit",) * 3 else [])


@pytest.mark.parametrize("reason", ["rate_limit", "access_denied", "invalid_key", "invalid_response", "connection_error", "keys_unavailable"])
def test_unknown_or_temporary_failure_never_spends_fns(db, monkeypatch, reason):
    run = SearchRun(status="pending", requested_okved_codes=["49.41"])
    db.add(run)
    db.commit()
    calls = []

    def stage(db, run, settings, limit, provider):
        calls.append(provider)
        raise DiscoveryAPIError("Stopped", stop_discovery=True, reason=reason)

    monkeypatch.setattr("app.services.discovery._run_provider_discovery", stage)
    monkeypatch.setattr("app.services.discovery.SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    run_discovery(run.id, Settings(_env_file=None, discovery_provider="combined", checko_api_key="c", api_fns_key="f"), 1)
    assert calls == ["checko"]


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


def test_per_run_budget_does_not_unlock_fns(db, monkeypatch):
    calls = []

    def stage(db, run, settings, limit, provider):
        calls.append(provider)
        run.errors_count += 1
        run.error_message = "Per-run cap reached"
        return True

    monkeypatch.setattr("app.services.discovery._run_provider_discovery", stage)
    monkeypatch.setattr("app.services.discovery.SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    run = SearchRun(status="pending", requested_okved_codes=["49.41"])
    db.add(run)
    db.commit()
    run_discovery(run.id, Settings(_env_file=None, discovery_provider="combined", okvedo_api_key="o", api_fns_key="f"), 1)
    assert calls == ["okvedo"]


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
