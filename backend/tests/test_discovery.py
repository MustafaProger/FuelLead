from sqlalchemy import func, select

from app.models import ActivityHistory, Company, CompanyEmail, CompanyOkved, SearchRun
from app.services.checko import CompanyPayload, OkvedItem
from app.services.discovery import (
    classify_activity,
    fail_interrupted_search_runs,
    is_target_region,
    sanitize_search_run_errors,
    upsert_company,
)


def make_payload(emails: list[str]) -> CompanyPayload:
    return CompanyPayload(
        name='ООО "ТЕСТ ТРАНС"',
        inn="7701234567",
        ogrn="1267700000000",
        primary_okved=OkvedItem("41.20", "Строительство зданий"),
        additional_okveds=[OkvedItem("49.41.2", "Грузовые перевозки")],
        emails=emails,
    )


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
