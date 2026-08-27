from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import SessionLocal
from app.models import ActivityHistory, Company, CompanyEmail, CompanyOkved, SearchRun
from app.services.checko import CheckoAPIError, CheckoClient, CompanyPayload, OkvedItem
from app.services.demo_data import DEMO_COMPANIES


CATEGORY_RULES = [
    ("freight", ("49.41", "52.21.2")),
    ("road_construction", ("42.11",)),
    ("agriculture", ("01",)),
    ("machinery", ("77.32", "77.39.1")),
    ("construction", ("41.20", "43.11", "43.12")),
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def fail_interrupted_search_runs(db: Session) -> int:
    interrupted = list(
        db.scalars(select(SearchRun).where(SearchRun.status.in_(("pending", "running")))).all()
    )
    if not interrupted:
        return 0

    completed_at = utcnow()
    for run in interrupted:
        run.status = "failed"
        run.errors_count += 1
        run.error_message = "Поиск прерван перезапуском приложения"
        run.completed_at = completed_at
    db.commit()
    return len(interrupted)


def classify_activity(payload: CompanyPayload) -> str:
    primary_code = payload.primary_okved.code if payload.primary_okved else None
    for category, prefixes in CATEGORY_RULES:
        if primary_code and any(primary_code.startswith(prefix) for prefix in prefixes):
            return category
    for item in payload.additional_okveds:
        for category, prefixes in CATEGORY_RULES:
            if any(item.code.startswith(prefix) for prefix in prefixes):
                return category
    return "other"


def add_history(
    db: Session,
    company: Company,
    event_type: str,
    description: str,
    *,
    from_status: str | None = None,
    to_status: str | None = None,
    event_data: dict | None = None,
) -> None:
    db.add(
        ActivityHistory(
            company=company,
            event_type=event_type,
            description=description,
            from_status=from_status,
            to_status=to_status,
            event_data=event_data,
        )
    )


def upsert_company(db: Session, payload: CompanyPayload, source: str = "Checko API") -> tuple[Company, bool]:
    if not payload.inn:
        raise ValueError("Company payload has no INN")

    now = utcnow()
    company = db.scalar(select(Company).where(Company.inn == payload.inn))
    created = company is None
    primary_code = payload.primary_okved.code if payload.primary_okved else None
    primary_name = payload.primary_okved.name if payload.primary_okved else None
    category = classify_activity(payload)

    if company is None:
        company = Company(
            name=payload.name,
            inn=payload.inn,
            ogrn=payload.ogrn,
            primary_okved_code=primary_code,
            primary_okved_name=primary_name,
            activity_category=category,
            is_active=payload.is_active,
            provider="checko",
            first_discovered_at=now,
            last_checked_at=now,
            last_updated_at=now,
        )
        db.add(company)
        db.flush()
        add_history(db, company, "company_discovered", "Компания обнаружена")
        add_history(db, company, "provider_data_received", "Данные компании получены из Checko")
    else:
        changed = any(
            [
                company.name != payload.name,
                company.ogrn != payload.ogrn,
                company.primary_okved_code != primary_code,
                company.primary_okved_name != primary_name,
                company.activity_category != category,
                company.is_active != payload.is_active,
            ]
        )
        company.name = payload.name
        company.ogrn = payload.ogrn
        company.primary_okved_code = primary_code
        company.primary_okved_name = primary_name
        company.activity_category = category
        company.is_active = payload.is_active
        company.last_checked_at = now
        if changed:
            company.last_updated_at = now
            add_history(db, company, "company_updated", "Данные компании обновлены из Checko")

    existing_okveds = {item.code: item for item in company.additional_okveds}
    incoming_okveds: dict[str, OkvedItem] = {}
    for item in payload.additional_okveds:
        code = item.code.strip()
        if code and code not in incoming_okveds:
            incoming_okveds[code] = item
    incoming_codes = set(incoming_okveds)
    for stale in [item for code, item in existing_okveds.items() if code not in incoming_codes]:
        db.delete(stale)
    for code, item in incoming_okveds.items():
        existing = existing_okveds.get(code)
        if existing:
            existing.name = item.name
        else:
            company.additional_okveds.append(CompanyOkved(code=code, name=item.name))

    existing_emails = {item.email.casefold() for item in company.emails}
    for email in payload.emails:
        normalized = email.casefold()
        if normalized not in existing_emails:
            company.emails.append(CompanyEmail(email=normalized, source=source))
            add_history(db, company, "email_discovered", f"Email {normalized} обнаружен", event_data={"email": normalized})
            existing_emails.add(normalized)
            company.last_updated_at = now

    db.flush()
    return company, created


def _update_run_counter(db: Session, run: SearchRun, *, created: bool) -> None:
    if created:
        run.companies_created += 1
    else:
        run.companies_updated += 1
    db.commit()


def run_discovery(run_id: int, settings: Settings, limit_per_code: int) -> None:
    db = SessionLocal()
    run = db.get(SearchRun, run_id)
    if run is None:
        db.close()
        return

    run.status = "running"
    run.started_at = utcnow()
    db.commit()

    try:
        if not settings.checko_configured:
            run.mode = "demo"
            demo_items = DEMO_COMPANIES[:limit_per_code]
            run.candidates_found = len(demo_items)
            for payload in demo_items:
                _, created = upsert_company(db, payload, source="Демонстрационные данные")
                _update_run_counter(db, run, created=created)
        else:
            run.mode = "checko"
            seen_inns: set[str] = set()
            with CheckoClient(
                settings.checko_api_key,
                settings.checko_base_url,
                settings.checko_timeout_seconds,
            ) as client:
                for code in run.requested_okved_codes:
                    try:
                        records = client.search_by_okved(code, limit=limit_per_code)
                    except (CheckoAPIError, httpx.HTTPError) as exc:
                        run.errors_count += 1
                        run.error_message = str(exc)
                        db.commit()
                        continue

                    for record in records:
                        inn = str(record.get("ИНН") or "")
                        if not inn or inn in seen_inns:
                            continue
                        seen_inns.add(inn)
                        run.candidates_found += 1
                        try:
                            payload = client.get_company(inn)
                            if not payload.is_active:
                                run.skipped_inactive += 1
                                db.commit()
                                continue
                            with db.begin_nested():
                                _, created = upsert_company(db, payload)
                            _update_run_counter(db, run, created=created)
                        except (CheckoAPIError, httpx.HTTPError, ValueError, SQLAlchemyError) as exc:
                            run.errors_count += 1
                            run.error_message = str(exc)
                            db.commit()

        run.status = "completed" if (run.companies_created + run.companies_updated) > 0 else "failed"
        if run.status == "failed" and not run.error_message:
            run.error_message = "Поиск не вернул компаний"
    except Exception as exc:  # keep the background task observable via the run record
        db.rollback()
        run = db.get(SearchRun, run_id)
        if run is not None:
            run.status = "failed"
            run.errors_count += 1
            run.error_message = str(exc)
    finally:
        if run is not None:
            run.completed_at = utcnow()
            db.commit()
        db.close()
