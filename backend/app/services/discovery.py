from datetime import datetime, timezone
import time
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import TARGET_REGION_CODES, Settings
from app.database import SessionLocal
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
from app.services.api_fns import ApiFnsClient
from app.services.checko import CheckoClient
from app.services.demo_data import DEMO_COMPANIES
from app.services.okvedo import OkvedoClient
from app.services.dadata import DaDataClient
from app.services.provider import (
    CompanyPayload,
    DiscoveryAPIError,
    DiscoveryClient,
    OkvedItem,
    SearchPage,
    redact_sensitive_url,
)


CATEGORY_RULES = [
    ("freight", ("49.41", "52.21.2")),
    ("road_construction", ("42.11",)),
    ("agriculture", ("01",)),
    ("machinery", ("77.32", "77.39.1")),
    ("construction", ("41.20", "43.11", "43.12")),
]
MAX_SEARCH_PAGES_PER_QUERY_RUN = 2
PROVIDER_LABELS = {"checko": "Checko", "okvedo": "Okvedo", "dadata": "DaData", "api_fns": "API-ФНС"}
T = TypeVar("T")


class SearchCancelled(Exception):
    pass


def _check_cancelled(db: Session, run: SearchRun) -> None:
    # Refresh only the flag so an API stop cannot be overwritten by stale state.
    db.refresh(run, attribute_names=["cancel_requested"])
    if run.cancel_requested:
        raise SearchCancelled()


def _wait_for_provider(db: Session, run: SearchRun, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        _check_cancelled(db, run)
        time.sleep(min(remaining, 1))


def _provider_call(db: Session, run: SearchRun, operation: str, call: Callable[[], T]) -> T:
    """Pace full runs and retry temporary throttles without restarting discovery."""
    full = run.search_scope == "full"
    message = run.progress_message
    for attempt in range(4):
        _check_cancelled(db, run)
        if full:
            # Below Okvedo free tier's 60 requests/minute, including search/cards.
            _wait_for_provider(db, run, 1.05)
        setattr(run, operation, getattr(run, operation) + 1)
        run.progress_message = message
        db.commit()
        try:
            return call()
        except DiscoveryAPIError as exc:
            if not full or exc.reason != "rate_limit" or attempt == 3:
                raise
            delay = max(60 * (attempt + 1), exc.retry_after_seconds or 0)
            run.progress_message = f"Провайдер ограничил частоту. Продолжим автоматически через {delay:g} сек."
            db.commit()
            _wait_for_provider(db, run, delay)
    raise AssertionError("Unreachable")


def _record_provider_result(db: Session, run: SearchRun, provider: str, reason: str) -> None:
    run.provider_results = {**run.provider_results, provider: reason}
    db.commit()


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


def upsert_company(
    db: Session,
    payload: CompanyPayload,
    source: str = "Checko API",
    provider: str = "checko",
) -> tuple[Company, bool]:
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
            provider=provider,
            first_discovered_at=now,
            last_checked_at=now,
            last_updated_at=now,
        )
        db.add(company)
        db.flush()
        add_history(db, company, "company_discovered", "Компания обнаружена")
        add_history(db, company, "provider_data_received", f"Данные компании получены: {source}")
    else:
        changed = any(
            [
                company.name != payload.name,
                company.ogrn != payload.ogrn,
                company.primary_okved_code != primary_code,
                company.primary_okved_name != primary_name,
                company.activity_category != category,
                company.is_active != payload.is_active,
                company.provider != provider,
            ]
        )
        company.name = payload.name
        company.ogrn = payload.ogrn
        company.primary_okved_code = primary_code
        company.primary_okved_name = primary_name
        company.activity_category = category
        company.is_active = payload.is_active
        company.provider = provider
        company.last_checked_at = now
        if changed:
            company.last_updated_at = now
            add_history(db, company, "company_updated", f"Данные компании обновлены: {source}")

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

    existing_phones = {
        item.value for item in company.contacts if item.contact_type == "phone"
    }
    for phone_number in payload.phone_numbers:
        if phone_number not in existing_phones:
            company.contacts.append(
                CompanyContact(
                    contact_type="phone",
                    value=phone_number,
                    source=source,
                )
            )
            add_history(
                db,
                company,
                "contact_discovered",
                f"Телефон {phone_number} обнаружен",
                event_data={"contact_type": "phone", "value": phone_number},
            )
            existing_phones.add(phone_number)
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
    provider: str,
    okved_code: str,
    region_code: str,
    page_size: int,
) -> DiscoveryCursor:
    cursor = db.scalar(
        select(DiscoveryCursor).where(
            DiscoveryCursor.provider == provider,
            DiscoveryCursor.okved_code == okved_code,
            DiscoveryCursor.region_code == region_code,
        )
    )
    # A cursor without a provider can only be a row created by an older
    # application version or by a direct data import. Adopt it once for API-FNS
    # instead of losing its position, while migrated production rows are already
    # explicitly marked as Checko.
    if cursor is None and provider == "api_fns":
        cursor = db.scalar(
            select(DiscoveryCursor).where(
                DiscoveryCursor.provider.is_(None),
                DiscoveryCursor.okved_code == okved_code,
                DiscoveryCursor.region_code == region_code,
            )
        )
        if cursor is not None:
            cursor.provider = provider
    if cursor is None:
        cursor = DiscoveryCursor(
            provider=provider,
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
    client: DiscoveryClient,
    limit_per_code: int,
    *,
    provider: str = "checko",
    source: str = "Checko API",
    max_search_requests: int | None = None,
    max_company_requests: int | None = None,
    propagate_stop_errors: bool = False,
) -> bool:
    """Discover unknown INNs while continuing through persistent provider pages.

    Returns True when the provider made further discovery impossible for this run.
    """
    known_inns = set(db.scalars(select(Company.inn)).all())
    known_inns.update(db.scalars(select(ExcludedCompany.inn)).all())
    seen_inns: set[str] = set()
    full = run.search_scope == "full"
    page_size = (getattr(client, "fixed_page_size", None) or 100) if full else limit_per_code
    cursor_page_size = getattr(client, "fixed_page_size", None) or page_size
    run.active_provider = provider
    db.commit()
    search_requests = 0
    company_requests = 0

    for code in run.requested_okved_codes:
        for region_code in TARGET_REGION_CODES:
            cursor = _get_discovery_cursor(
                db,
                provider=provider,
                okved_code=code,
                region_code=region_code,
                page_size=cursor_page_size,
            )
            candidate_attempts = 0
            pages_scanned = 0
            visited_pages: set[int] = set()
            page_fingerprints: set[tuple[str, ...]] = set()

            while (
                full or (candidate_attempts < limit_per_code
                and pages_scanned < MAX_SEARCH_PAGES_PER_QUERY_RUN)
            ):
                _check_cancelled(db, run)
                requested_page = cursor.next_page
                if requested_page in visited_pages:
                    raise DiscoveryAPIError("Провайдер повторяет страницу выдачи. Позиция сохранена.",
                                            stop_discovery=True, reason="pagination_stalled")
                run.progress_message = f"{PROVIDER_LABELS[provider]} · ОКВЭД {code} · регион {region_code} · страница {requested_page}"
                db.commit()
                if max_search_requests is not None and search_requests >= max_search_requests:
                    run.errors_count += 1
                    run.error_message = (
                        "Достигнут безопасный лимит API-ФНС. "
                        f"Лимит search на один запуск: {max_search_requests}. "
                        "Курсор сохранён для продолжения."
                    )
                    db.commit()
                    return True
                search_requests += 1
                try:
                    search_page = _provider_call(db, run, "search_requests", lambda: client.search_by_okved(
                        code, region_code=region_code, limit=page_size, page=requested_page,
                    ))
                except DiscoveryAPIError as exc:
                    if exc.stop_discovery and propagate_stop_errors:
                        db.commit()
                        raise
                    run.errors_count += 1
                    run.error_message = str(exc)
                    db.commit()
                    if exc.stop_discovery:
                        return True
                    break

                pages_scanned += 1
                fingerprint = tuple(str(record.get("ИНН") or "") for record in search_page.records)
                if full and fingerprint and fingerprint in page_fingerprints:
                    raise DiscoveryAPIError("Провайдер повторяет содержимое страницы. Позиция сохранена.",
                                            stop_discovery=True, reason="pagination_stalled")
                page_fingerprints.add(fingerprint)
                if search_page.current_page in visited_pages:
                    raise DiscoveryAPIError("Провайдер повторяет страницу выдачи. Позиция сохранена.",
                                            stop_discovery=True, reason="pagination_stalled")
                visited_pages.add(search_page.current_page)
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

                    if max_company_requests is not None and company_requests >= max_company_requests:
                        run.errors_count += 1
                        run.error_message = (
                            "Достигнут безопасный лимит API-ФНС. "
                            f"Лимит egr на один запуск: {max_company_requests}. "
                            "Курсор сохранён для продолжения."
                        )
                        db.commit()
                        return True
                    _check_cancelled(db, run)
                    seen_inns.add(inn)
                    run.candidates_found += 1
                    candidate_attempts += 1
                    company_requests += 1
                    try:
                        payload = _provider_call(db, run, "company_requests", lambda: client.get_company(inn))
                        if not payload.is_active:
                            run.skipped_inactive += 1
                        elif is_target_region(payload):
                            with db.begin_nested():
                                _, created = upsert_company(
                                    db,
                                    payload,
                                    source=source,
                                    provider=provider,
                                )
                            _advance_cursor_after_record(cursor, search_page, record_index + 1)
                            if created:
                                known_inns.add(inn)
                            _update_run_counter(db, run, created=created)
                            wrapped_cycle = cursor.next_page == 1 and cursor.next_record_index == 0
                            if not full and candidate_attempts >= limit_per_code:
                                break
                            continue
                    except DiscoveryAPIError as exc:
                        if exc.reason == "not_found":
                            # A permanently absent card must not pin this query
                            # to the same INN on every subsequent click.
                            run.errors_count += 1
                            run.error_message = str(exc)
                            wrapped_cycle = _advance_cursor_after_record(cursor, search_page, record_index + 1)
                            db.commit()
                            if not full and candidate_attempts >= limit_per_code:
                                break
                            continue
                        if exc.stop_discovery and propagate_stop_errors:
                            db.commit()
                            raise
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
                    if not full and candidate_attempts >= limit_per_code:
                        break

                if retry_current_record:
                    break
                if not search_page.records:
                    cursor.next_page = 1
                    cursor.next_record_index = 0
                    db.commit()
                    break
                db.commit()
                if (not full and candidate_attempts >= limit_per_code) or wrapped_cycle:
                    break

    return False


def _run_provider_discovery(
    db: Session,
    run: SearchRun,
    settings: Settings,
    limit_per_code: int,
    provider: str,
) -> bool:
    if provider == "checko":
        if not settings.checko_configured:
            raise DiscoveryAPIError(
                "Выбран провайдер Checko, но CHECKO_API_KEY не настроен в локальном .env.",
                stop_discovery=True,
            )
        with CheckoClient(
            settings.checko_api_keys,
            settings.checko_base_url,
            settings.checko_timeout_seconds,
        ) as client:
            return discover_new_companies(
                db,
                run,
                client,
                limit_per_code,
                provider="checko",
                source="Checko API",
                propagate_stop_errors=True,
            )

    if provider in ("okvedo", "dadata"):
        label = "Okvedo" if provider == "okvedo" else "DaData"
        if not getattr(settings, f"{provider}_configured"):
            raise DiscoveryAPIError(
                f"Выбран {label}, но {provider.upper()}_API_KEY не настроен в локальном .env.",
                stop_discovery=True,
            )
        client_type = OkvedoClient if provider == "okvedo" else DaDataClient
        options = {"secret_key": settings.dadata_secret_key} if provider == "dadata" else {}
        with client_type(
            getattr(settings, f"{provider}_api_key"),
            getattr(settings, f"{provider}_base_url"),
            getattr(settings, f"{provider}_timeout_seconds"),
            **options,
        ) as client:
            return discover_new_companies(
                db, run, client, limit_per_code,
                provider=provider, source=f"{label} API", propagate_stop_errors=True,
            )

    if provider == "api_fns":
        if not settings.api_fns_configured:
            raise DiscoveryAPIError(
                "Выбран провайдер API-ФНС, но API_FNS_KEY не настроен в локальном .env.",
                stop_discovery=True,
            )
        with ApiFnsClient(
            settings.api_fns_key,
            settings.api_fns_base_url,
            settings.api_fns_timeout_seconds,
            require_phone=settings.api_fns_require_phone,
            require_email=settings.api_fns_require_email,
        ) as client:
            return discover_new_companies(
                db,
                run,
                client,
                limit_per_code,
                provider="api_fns",
                source="API-ФНС",
                max_search_requests=None if run.search_scope == "full" else settings.api_fns_max_search_requests_per_run,
                max_company_requests=None if run.search_scope == "full" else settings.api_fns_max_egr_requests_per_run,
                propagate_stop_errors=True,
            )

    raise ValueError(f"Unsupported discovery provider: {provider}")


def _run_combined_discovery(
    db: Session,
    run: SearchRun,
    settings: Settings,
    limit_per_code: int,
) -> bool:
    """Use API-FNS only after every configured primary reports exhausted quota.

    Empty results, a per-run budget, bad credentials and transient failures are
    not proof of exhausted quota. Checko must exhaust every configured key.
    """
    labels = {"checko": "Checko", "okvedo": "Okvedo", "dadata": "DaData", "api_fns": "API-ФНС"}
    error_messages: list[str] = []
    successful_stages = 0
    all_primary_quotas_exhausted = True
    providers = list(settings.primary_discovery_providers)
    if settings.api_fns_configured:
        providers.append("api_fns")
    if not providers:
        raise DiscoveryAPIError("Не настроен ни один API. Добавьте ключи в локальный .env.")

    for provider in providers:
        _check_cancelled(db, run)
        if provider == "api_fns" and not all_primary_quotas_exhausted:
            _record_provider_result(db, run, provider, "reserve_not_needed")
            continue
        run.active_provider = provider
        run.progress_message = f"Подключаем {PROVIDER_LABELS[provider]}"
        db.commit()
        errors_before = run.errors_count
        run.error_message = None
        try:
            _run_provider_discovery(db, run, settings, limit_per_code, provider)
        except DiscoveryAPIError as exc:
            run.errors_count += 1
            error_messages.append(f"{labels[provider]}: {exc}")
            if provider != "api_fns" and exc.reason != "daily_limit":
                all_primary_quotas_exhausted = False
            db.commit()
            _record_provider_result(db, run, provider, exc.reason or "error")
            continue

        if provider != "api_fns":
            all_primary_quotas_exhausted = False
        if run.errors_count == errors_before:
            successful_stages += 1
        elif run.error_message:
            error_messages.append(f"{labels[provider]}: {run.error_message}")
        _record_provider_result(db, run, provider, "results_exhausted" if run.errors_count == errors_before else "partial")

    run.error_message = "\n".join(error_messages) or None
    db.commit()
    return successful_stages > 0 or (run.companies_created + run.companies_updated) > 0


def run_discovery(run_id: int, settings: Settings, limit_per_code: int) -> None:
    db = SessionLocal()
    run = db.get(SearchRun, run_id)
    if run is None or run.status != "pending":
        db.close()
        return

    provider = settings.resolved_discovery_provider
    run.status = "running"
    run.mode = provider
    run.started_at = utcnow()
    db.commit()

    try:
        _check_cancelled(db, run)
        if provider == "demo":
            run.mode = "demo"
            excluded_inns = set(db.scalars(select(ExcludedCompany.inn)).all())
            demo_items = [
                payload for payload in DEMO_COMPANIES if payload.inn not in excluded_inns
            ]
            if run.search_scope != "full":
                demo_items = demo_items[:limit_per_code]
            run.candidates_found = len(demo_items)
            for payload in demo_items:
                _check_cancelled(db, run)
                _, created = upsert_company(
                    db,
                    payload,
                    source="Демонстрационные данные",
                    provider="demo",
                )
                _update_run_counter(db, run, created=created)
        elif provider == "combined":
            combined_succeeded = _run_combined_discovery(
                db,
                run,
                settings,
                limit_per_code,
            )
            run.status = "completed" if combined_succeeded else "failed"
        else:
            run.active_provider = provider
            _run_provider_discovery(db, run, settings, limit_per_code, provider)
            _record_provider_result(db, run, provider, "results_exhausted" if run.errors_count == 0 else "partial")

        if provider != "combined":
            has_processed_companies = (run.companies_created + run.companies_updated) > 0
            run.status = "completed" if has_processed_companies or run.errors_count == 0 else "failed"
    except SearchCancelled:
        run.status = "cancelled"
    except DiscoveryAPIError as exc:
        run.errors_count += 1
        run.error_message = str(exc)
        run.status = "completed" if run.companies_created + run.companies_updated else "failed"
        _record_provider_result(db, run, provider, exc.reason or "error")
    except Exception as exc:  # keep the background task observable via the run record
        db.rollback()
        run = db.get(SearchRun, run_id)
        if run is not None:
            run.status = "failed"
            run.errors_count += 1
            run.error_message = redact_sensitive_url(str(exc))
    finally:
        if run is not None:
            db.refresh(run, attribute_names=["cancel_requested"])
            if run.cancel_requested:
                run.status = "cancelled"
            run.progress_message = (
                "Поиск остановлен. Найденные компании и позиция сохранены."
                if run.status == "cancelled" else "Проход завершён. Следующий запуск продолжит с сохранённых позиций."
            )
            run.active_provider = None
            run.completed_at = utcnow()
            db.commit()
        db.close()
