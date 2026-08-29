from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.models import (
    ActivityHistory,
    Company,
    CompanyContact,
    CompanyEmail,
    CompanyOkved,
    DiscoveryCursor,
    ExcludedCompany,
    SearchRun,
)
from app.services.checko import CompanyPayload, OkvedItem, SearchPage
from app.services.discovery import (
    classify_activity,
    discover_new_companies,
    fail_interrupted_search_runs,
    is_target_region,
    run_discovery,
    sanitize_search_run_errors,
    upsert_company,
)
from app.services.demo_data import DEMO_COMPANIES


def make_payload(emails: list[str]) -> CompanyPayload:
    return CompanyPayload(
        name='ООО "ТЕСТ ТРАНС"',
        inn="7701234567",
        ogrn="1267700000000",
        primary_okved=OkvedItem("41.20", "Строительство зданий"),
        additional_okveds=[OkvedItem("49.41.2", "Грузовые перевозки")],
        emails=emails,
    )


class FakePaginatedCheckoClient:
    def __init__(self, inns: list[str]):
        self.inns = inns
        self.search_calls: list[tuple[str, str, int, int]] = []
        self.company_calls: list[str] = []

    def search_by_okved(
        self,
        code: str,
        *,
        region_code: str,
        limit: int,
        page: int,
    ) -> SearchPage:
        self.search_calls.append((code, region_code, limit, page))
        if region_code == "50":
            return SearchPage(records=[], current_page=1, total_pages=1)
        start = (page - 1) * limit
        page_inns = self.inns[start : start + limit]
        total_pages = max((len(self.inns) + limit - 1) // limit, 1)
        return SearchPage(
            records=[{"ИНН": inn, "РегионКод": "77"} for inn in page_inns],
            current_page=page,
            total_pages=total_pages,
        )

    def get_company(self, inn: str) -> CompanyPayload:
        self.company_calls.append(inn)
        payload = make_payload([])
        payload.inn = inn
        payload.name = f'ООО "КОМПАНИЯ {inn}"'
        payload.region_code = "77"
        return payload


def test_category_uses_additional_okved():
    payload = make_payload([])
    payload.primary_okved = OkvedItem("46.90", "Оптовая торговля")
    assert classify_activity(payload) == "freight"


def test_category_prefers_relevant_primary_okved():
    assert classify_activity(make_payload([])) == "construction"


def test_target_region_accepts_only_moscow_and_moscow_oblast():
    payload = make_payload([])
    payload.region_code = "77"
    assert is_target_region(payload) is True
    payload.region_code = "50"
    assert is_target_region(payload) is True
    payload.region_code = "01"
    assert is_target_region(payload) is False
    payload.region_code = None
    assert is_target_region(payload) is False


def test_upsert_deduplicates_by_inn_and_email(db):
    first, created = upsert_company(db, make_payload(["info@example.ru"]))
    db.commit()
    original_discovery = first.first_discovered_at

    second, created_again = upsert_company(
        db, make_payload(["info@example.ru", "office@example.ru"])
    )
    db.commit()

    assert created is True
    assert created_again is False
    assert second.id == first.id
    assert second.first_discovered_at == original_discovery
    assert db.scalar(select(func.count(Company.id))) == 1
    assert db.scalar(select(func.count(CompanyEmail.id))) == 2
    assert db.scalar(select(func.count(ActivityHistory.id))) == 4


def test_upsert_deduplicates_repeated_additional_okveds(db):
    payload = make_payload([])
    payload.additional_okveds = [
        OkvedItem("42.11", "Строительство дорог"),
        OkvedItem("42.11", "Повтор из ответа провайдера"),
    ]

    upsert_company(db, payload)
    db.commit()

    okved = db.scalar(select(CompanyOkved))
    assert db.scalar(select(func.count(CompanyOkved.id))) == 1
    assert okved.code == "42.11"
    assert okved.name == "Строительство дорог"


def test_upsert_adds_provider_phones_without_duplicates(db):
    payload = make_payload([])
    payload.phone_numbers = ["+74951234567", "+74951234567"]

    upsert_company(db, payload)
    db.commit()
    upsert_company(db, payload)
    db.commit()

    contact = db.scalar(select(CompanyContact))
    assert db.scalar(select(func.count(CompanyContact.id))) == 1
    assert contact.contact_type == "phone"
    assert contact.value == "+74951234567"
    assert contact.source == "Checko API"


def test_interrupted_search_runs_are_marked_failed(db):
    pending = SearchRun(status="pending", requested_okved_codes=["42.11"])
    running = SearchRun(status="running", requested_okved_codes=["49.41"])
    completed = SearchRun(status="completed", requested_okved_codes=["01"])
    db.add_all([pending, running, completed])
    db.commit()

    assert fail_interrupted_search_runs(db) == 2

    assert pending.status == "failed"
    assert pending.errors_count == 1
    assert pending.completed_at is not None
    assert pending.error_message == "Поиск прерван перезапуском приложения"
    assert running.status == "failed"
    assert completed.status == "completed"


def test_stored_search_errors_are_redacted(db):
    run = SearchRun(
        status="failed",
        requested_okved_codes=["49.41"],
        error_message="Client error for https://api.checko.ru/v2/search?key=secret-value&by=okved",
    )
    db.add(run)
    db.commit()

    assert sanitize_search_run_errors(db) == 1
    assert "secret-value" not in run.error_message
    assert "key=<redacted>" in run.error_message


def test_five_consecutive_searches_create_five_different_companies(db):
    inns = [f"770100000{index}" for index in range(1, 6)]
    client = FakePaginatedCheckoClient(inns)
    session_factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)

    created_per_run = []
    for _ in range(5):
        with session_factory() as run_db:
            run = SearchRun(status="running", requested_okved_codes=["49.41"])
            run_db.add(run)
            run_db.commit()

            assert discover_new_companies(run_db, run, client, limit_per_code=1) is False
            created_per_run.append(run.companies_created)

    db.expire_all()
    assert created_per_run == [1, 1, 1, 1, 1]
    assert db.scalar(select(func.count(Company.id))) == 5
    assert client.company_calls == inns
    assert [call[3] for call in client.search_calls if call[1] == "77"] == [1, 2, 3, 4, 5]

    cursor = db.scalar(
        select(DiscoveryCursor).where(
            DiscoveryCursor.okved_code == "49.41",
            DiscoveryCursor.region_code == "77",
        )
    )
    assert cursor.next_page == 1
    assert cursor.completed_cycles == 1


def test_known_inn_is_skipped_before_company_request_and_next_page_is_used(db):
    known_payload = make_payload([])
    known_payload.inn = "7701000001"
    upsert_company(db, known_payload)
    db.commit()

    client = FakePaginatedCheckoClient(["7701000001", "7701000002"])
    run = SearchRun(status="running", requested_okved_codes=["49.41"])
    db.add(run)
    db.commit()

    assert discover_new_companies(db, run, client, limit_per_code=1) is False

    assert run.companies_created == 1
    assert run.companies_updated == 0
    assert client.company_calls == ["7701000002"]
    assert [call[3] for call in client.search_calls if call[1] == "77"] == [1, 2]


def test_excluded_inn_is_skipped_before_company_request(db):
    db.add(ExcludedCompany(inn="7701000001", name='ООО "УДАЛЕНА"'))
    run = SearchRun(status="running", requested_okved_codes=["49.41"])
    db.add(run)
    db.commit()
    client = FakePaginatedCheckoClient(["7701000001"])

    assert discover_new_companies(db, run, client, limit_per_code=1) is False

    assert run.companies_created == 0
    assert client.company_calls == []


def test_demo_discovery_also_skips_excluded_inn(db, monkeypatch):
    excluded_inn = DEMO_COMPANIES[0].inn
    db.add(ExcludedCompany(inn=excluded_inn, name=DEMO_COMPANIES[0].name))
    run = SearchRun(status="pending", requested_okved_codes=["49.41"])
    db.add(run)
    db.commit()
    session_factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    monkeypatch.setattr("app.services.discovery.SessionLocal", session_factory)

    run_discovery(run.id, Settings(_env_file=None, checko_api_key=""), limit_per_code=1)

    db.expire_all()
    stored_run = db.get(SearchRun, run.id)
    assert stored_run.status == "completed"
    assert stored_run.companies_created == 1
    assert db.scalar(select(Company.inn)) != excluded_inn
