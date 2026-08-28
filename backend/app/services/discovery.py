from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import TARGET_REGION_CODES, Settings
from app.database import SessionLocal
from app.models import (
    ActivityHistory,
    Company,
    CompanyEmail,
    CompanyOkved,
    DiscoveryCursor,
    SearchRun,
)
from app.services.checko import (
    CheckoAPIError,
    CheckoClient,
    CompanyPayload,
    OkvedItem,
    SearchPage,
    redact_sensitive_url,
)
from app.services.demo_data import DEMO_COMPANIES


CATEGORY_RULES = [
    ("freight", ("49.41", "52.21.2")),
    ("road_construction", ("42.11",)),
    ("agriculture", ("01",)),
    ("machinery", ("77.32", "77.39.1")),
    ("construction", ("41.20", "43.11", "43.12")),
]
MAX_SEARCH_PAGES_PER_QUERY_RUN = 2


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


def sanitize_search_run_errors(db: Session) -> int:
    runs = list(
        db.scalars(select(SearchRun).where(SearchRun.error_message.contains("key="))).all()
    )
    changed = 0
    for run in runs:
        sanitized = redact_sensitive_url(run.error_message or "")
        if sanitized != run.error_message:
            run.error_message = sanitized
            changed += 1
    if changed:
        db.commit()
    return changed


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


def is_target_region(payload: CompanyPayload) -> bool:
    return payload.region_code in TARGET_REGION_CODES


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


def _get_discovery_cursor(
    db: Session,
    *,
    okved_code: str,
    region_code: str,
    page_size: int,
) -> DiscoveryCursor:
    cursor = db.scalar(
        select(DiscoveryCursor).where(
            DiscoveryCursor.okved_code == okved_code,
            DiscoveryCursor.region_code == region_code,
        )
    )
    if cursor is None:
        cursor = DiscoveryCursor(
            okved_code=okved_code,
            region_code=region_code,
            page_size=page_size,
        )
        db.add(cursor)
        db.flush()
        return cursor

    if cursor.page_size != page_size:
        absolute_offset = max(cursor.next_page - 1, 0) * cursor.page_size
        absolute_offset += max(cursor.next_record_index, 0)
        cursor.next_page = absolute_offset // page_size + 1
        cursor.next_record_index = absolute_offset % page_size
        cursor.page_size = page_size
    return cursor


def _advance_cursor_after_record(
    cursor: DiscoveryCursor,
    search_page: SearchPage,
    next_record_index: int,
) -> bool:
    """Advance the cursor and return True when the result cycle wrapped to page one."""
    if next_record_index < len(search_page.records):
        cursor.next_page = search_page.current_page
        cursor.next_record_index = next_record_index
        return False

    cursor.next_record_index = 0
    if search_page.current_page < search_page.total_pages:
        cursor.next_page = search_page.current_page + 1
        return False

    cursor.next_page = 1
    cursor.completed_cycles += 1
    return True


def discover_new_companies(
    db: Session,
    run: SearchRun,
    client: CheckoClient,
    limit_per_code: int,
) -> bool:
    """Discover only unknown INNs while continuing through persistent Checko pages.

    Returns True when the provider made further discovery impossible for this run.
    """
    known_inns = set(db.scalars(select(Company.inn)).all())
    seen_inns: set[str] = set()

    for code in run.requested_okved_codes:
        for region_code in TARGET_REGION_CODES:
            cursor = _get_discovery_cursor(
                db,
                okved_code=code,
                region_code=region_code,
                page_size=limit_per_code,
            )
            candidate_attempts = 0
            pages_scanned = 0

            while (
                candidate_attempts < limit_per_code
                and pages_scanned < MAX_SEARCH_PAGES_PER_QUERY_RUN
            ):
                requested_page = cursor.next_page
                try:
                    search_page = client.search_by_okved(
                        code,
                        region_code=region_code,
                        limit=limit_per_code,
                        page=requested_page,
                    )
                except CheckoAPIError as exc:
                    run.errors_count += 1
                    run.error_message = str(exc)
                    db.commit()
                    if exc.stop_discovery:
                        return True
                    break

                pages_scanned += 1
                if search_page.current_page != requested_page:
                    cursor.next_page = search_page.current_page
                    cursor.next_record_index = 0

                start_index = min(cursor.next_record_index, len(search_page.records))
                wrapped_cycle = (
                    _advance_cursor_after_record(cursor, search_page, start_index)
                    if start_index == len(search_page.records) and search_page.records
                    else False
                )
                retry_current_record = False

                for record_index in range(start_index, len(search_page.records)):
                    record = search_page.records[record_index]
                    record_region = str(record.get("РегионКод") or "").strip()
                    inn = str(record.get("ИНН") or "").strip()

                    if record_region and record_region not in TARGET_REGION_CODES:
                        wrapped_cycle = _advance_cursor_after_record(
                            cursor, search_page, record_index + 1
                        )
                        continue
                    if not inn or inn in known_inns or inn in seen_inns:
                        wrapped_cycle = _advance_cursor_after_record(
                            cursor, search_page, record_index + 1
                        )
                        continue

                    seen_inns.add(inn)
                    run.candidates_found += 1
                    candidate_attempts += 1
                    try:
                        payload = client.get_company(inn)
                        if not payload.is_active:
                            run.skipped_inactive += 1
                        elif is_target_region(payload):
                            with db.begin_nested():
                                _, created = upsert_company(db, payload)
                            _advance_cursor_after_record(cursor, search_page, record_index + 1)
                            if created:
                                known_inns.add(inn)
                            _update_run_counter(db, run, created=created)
                            wrapped_cycle = cursor.next_page == 1 and cursor.next_record_index == 0
                            if candidate_attempts >= limit_per_code:
                                break
                            continue
                    except CheckoAPIError as exc:
                        run.errors_count += 1
                        run.error_message = str(exc)
                        db.commit()
                        retry_current_record = True
                        if exc.stop_discovery:
                            return True
                        break
                    except SQLAlchemyError as exc:
                        run.errors_count += 1
                        run.error_message = str(exc)
                        db.commit()
                        retry_current_record = True
                        break
                    except ValueError as exc:
                        run.errors_count += 1
                        run.error_message = str(exc)

                    wrapped_cycle = _advance_cursor_after_record(
                        cursor, search_page, record_index + 1
                    )
                    db.commit()
                    if candidate_attempts >= limit_per_code:
                        break

                if retry_current_record:
                    break
                if not search_page.records:
                    cursor.next_page = 1
                    cursor.next_record_index = 0
                    db.commit()
                    break
                db.commit()
                if candidate_attempts >= limit_per_code or wrapped_cycle:
                    break

    return False


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
            with CheckoClient(
                settings.checko_api_keys,
                settings.checko_base_url,
                settings.checko_timeout_seconds,
            ) as client:
                discover_new_companies(db, run, client, limit_per_code)

        has_processed_companies = (run.companies_created + run.companies_updated) > 0
        run.status = "completed" if has_processed_companies or run.errors_count == 0 else "failed"
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
