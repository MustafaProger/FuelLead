import asyncio
from contextlib import asynccontextmanager, suppress
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import AUTH_COOKIE_NAME, create_session_token, credentials_match, session_email
from app.config import DEFAULT_OKVED_CODES, TARGET_REGION_CODES, Settings, get_settings
from app.database import SessionLocal, create_database, get_db
from app.export import build_xlsx
from app.models import (
    ALL_STATUSES,
    ActivityHistory,
    COMPANY_STATUS_LABELS,
    Company,
    CompanyContact,
    ExcludedCompany,
    SearchRun,
)
from app.queries import build_company_query
from app.schemas import (
    AuthLoginRequest,
    CompanyFilters,
    ContactCreate,
    EmailPreviewRequest,
    EmailSendRequest,
    EmailTemplateUpdate,
    OutreachCampaignCreate,
    OutreachPreflightRequest,
    SearchRunCreate,
    StatusUpdate,
)
from app.serializers import as_aware, company_to_dict, search_run_to_dict
from app.services.contacts import CONTACT_TYPE_LABELS, normalize_contact_value
from app.services.discovery import fail_interrupted_search_runs, run_discovery, sanitize_search_run_errors
from app.services.email_templates import (
    company_template_values,
    email_template_to_dict,
    get_or_create_email_template,
    render_email_template,
)
from app.services.gmail import GmailOAuthConfig, GmailOAuthError, GmailOAuthSender
from app.services.outreach import (
    OutreachPolicyError,
    active_outreach_campaign,
    append_opt_out_footer,
    assert_manual_send_allowed,
    build_outreach_preflight,
    cancel_outreach_campaign,
    create_outreach_campaign,
    mark_company_send_failed,
    outreach_campaign_to_dict,
    outreach_policy_dict,
    pause_outreach_campaign,
    recover_interrupted_outreach,
    resume_outreach_campaign,
    run_outreach_worker,
    wake_outreach_worker,
)
from app.services.provider import normalize_email


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_database()
    with SessionLocal() as db:
        fail_interrupted_search_runs(db)
        sanitize_search_run_errors(db)
        recover_interrupted_outreach(db)
    worker = asyncio.create_task(run_outreach_worker(get_settings()))
    try:
        yield
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


app = FastAPI(title="FuelLead API", version="0.1.0", lifespan=lifespan)
# 27 complete Monday-to-Sunday weeks form a six-month heatmap with no
# partial-week placeholders at either edge.
DASHBOARD_HISTORY_DAYS = 189


@app.middleware("http")
async def require_authentication(request: Request, call_next):
    if request.method == "OPTIONS" or not request.url.path.startswith("/api/"):
        return await call_next(request)
    if request.url.path == "/api/auth/login":
        return await call_next(request)
    email = session_email(request.cookies.get(AUTH_COOKIE_NAME), get_settings())
    if email is None:
        return JSONResponse(status_code=401, content={"detail": "Требуется авторизация"})
    request.state.auth_email = email
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/auth/login")
def login(
    credentials: AuthLoginRequest,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> dict:
    if not settings.auth_configured:
        raise HTTPException(status_code=503, detail="Авторизация не настроена")
    if not credentials_match(credentials.email, credentials.password, settings):
        raise HTTPException(status_code=401, detail="Неверная почта или пароль")
    token = create_session_token(settings.fuellead_auth_email.strip(), settings)
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=settings.fuellead_auth_cookie_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.fuellead_auth_cookie_secure,
        samesite="strict",
        path="/",
    )
    return {"authenticated": True, "email": settings.fuellead_auth_email.strip()}


@app.get("/api/auth/session")
def auth_session(request: Request) -> dict:
    return {"authenticated": True, "email": request.state.auth_email}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict:
    response.delete_cookie(key=AUTH_COOKIE_NAME, path="/", samesite="strict")
    return {"authenticated": False}


def filters_dependency(
    status: str | None = None,
    has_email: bool | None = None,
    email_provider: str | None = None,
    category: str | None = None,
    discovered_on: date | None = None,
    search: str | None = None,
) -> CompanyFilters:
    return CompanyFilters(
        status=status,
        has_email=has_email,
        email_provider=email_provider,
        category=category,
        discovered_on=discovered_on,
        search=search,
    )


@app.get("/api/health")
def health(settings: Settings = Depends(get_settings)) -> dict:
    selected_provider = settings.resolved_discovery_provider
    checko_state = (
        "selected"
        if selected_provider in ("checko", "combined") and settings.checko_configured
        else "standby"
        if settings.checko_configured
        else "not_configured"
    )
    return {
        "status": "ok",
        "app": settings.app_name,
        "checko_configured": settings.checko_configured,
        "checko_api_key_count": len(settings.checko_api_keys),
        "checko_state": checko_state,
        "api_fns_configured": settings.api_fns_configured,
        "api_fns_request_budget_per_run": {
            "search": settings.api_fns_max_search_requests_per_run,
            "egr": settings.api_fns_max_egr_requests_per_run,
        },
        "selected_discovery_provider": selected_provider,
        "mode": selected_provider,
        "default_okved_codes": DEFAULT_OKVED_CODES,
        "target_region_codes": TARGET_REGION_CODES,
        "discovery_limit_per_code": settings.discovery_limit_per_code,
        "outreach_sender_email": settings.outreach_sender_email,
        "gmail_auth_mode": "oauth2",
        "gmail_oauth_configured": settings.gmail_oauth_configured,
        "outreach_policy": outreach_policy_dict(settings),
    }


@app.get("/api/dashboard")
def dashboard(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    total = db.scalar(select(func.count(Company.id))) or 0
    with_email = db.scalar(select(func.count(Company.id)).where(Company.emails.any())) or 0
    status_rows = db.execute(select(Company.status, func.count(Company.id)).group_by(Company.status))
    status_counts = {status: 0 for status in ALL_STATUSES}
    status_counts.update({status: count for status, count in status_rows})
    sent_emails = db.scalar(
        select(func.count(ActivityHistory.id)).where(ActivityHistory.event_type == "email_sent")
    ) or 0

    local_today = datetime.now(settings.timezone).date()
    current_week_start = local_today - timedelta(days=local_today.weekday())
    first_day = current_week_start - timedelta(weeks=26)
    local_start = datetime.combine(first_day, time.min, tzinfo=settings.timezone)
    discovered_values = db.scalars(
        select(Company.first_discovered_at).where(
            Company.first_discovered_at >= local_start.astimezone(timezone.utc)
        )
    ).all()
    daily_counts = Counter(
        as_aware(value).astimezone(settings.timezone).date()
        for value in discovered_values
    )
    daily_discoveries = [
        {"date": (first_day + timedelta(days=offset)).isoformat(), "count": daily_counts[first_day + timedelta(days=offset)]}
        for offset in range(DASHBOARD_HISTORY_DAYS)
    ]

    recent_companies = list(
        db.scalars(
            select(Company).order_by(Company.first_discovered_at.desc(), Company.id.desc()).limit(5)
        ).all()
    )
    return {
        "metrics": {
            "total": total,
            "with_email": with_email,
            "new": status_counts["new"],
            "sent_emails": sent_emails,
            "interested": status_counts["interested"],
        },
        "status_counts": status_counts,
        "daily_discoveries": daily_discoveries,
        "recent_companies": [company_to_dict(company, settings) for company in recent_companies],
    }


@app.get("/api/companies")
def list_companies(
    filters: CompanyFilters = Depends(filters_dependency),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    base = build_company_query(filters, settings.timezone)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    companies = list(
        db.scalars(
            base.order_by(Company.first_discovered_at.desc(), Company.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )

    total_companies = db.scalar(select(func.count(Company.id))) or 0
    new_companies = db.scalar(select(func.count(Company.id)).where(Company.status == "new")) or 0
    with_email = db.scalar(select(func.count(Company.id)).where(Company.emails.any())) or 0
    stats = {
        "total": total_companies,
        "new": new_companies,
        "with_email": with_email,
        "without_email": total_companies - with_email,
    }
    return {
        "items": [company_to_dict(company, settings) for company in companies],
        "total": total,
        "page": page,
        "page_size": page_size,
        "stats": stats,
    }


@app.get("/api/companies/{company_id}")
def get_company(
    company_id: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    return company_to_dict(company, settings, detailed=True)


@app.patch("/api/companies/{company_id}/status")
def update_company_status(
    company_id: int,
    update: StatusUpdate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    if update.status not in ALL_STATUSES:
        raise HTTPException(status_code=422, detail="Недоступный статус")
    if company.status != update.status:
        previous = company.status
        company.status = update.status
        company.last_updated_at = datetime.now(timezone.utc)
        db.add(
            ActivityHistory(
                company=company,
                event_type="status_changed",
                description=(
                    "Статус изменён: "
                    f"{COMPANY_STATUS_LABELS[previous]} → "
                    f"{COMPANY_STATUS_LABELS[update.status]}"
                ),
                from_status=previous,
                to_status=update.status,
            )
        )
        db.commit()
        if update.status == "new" and settings.outreach_automatic_send_enabled:
            wake_outreach_worker()
    return company_to_dict(company, settings, detailed=True)


@app.post("/api/companies/{company_id}/contacts", status_code=201)
def add_company_contact(
    company_id: int,
    contact: ContactCreate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    try:
        normalized = normalize_contact_value(contact.contact_type, contact.value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if any(
        item.contact_type == contact.contact_type and item.value == normalized
        for item in company.contacts
    ):
        raise HTTPException(status_code=409, detail="Такой контакт уже добавлен")

    company.contacts.append(
        CompanyContact(
            contact_type=contact.contact_type,
            value=normalized,
            source="Вручную",
        )
    )
    company.last_updated_at = datetime.now(timezone.utc)
    db.add(
        ActivityHistory(
            company=company,
            event_type="contact_added",
            description=f"{CONTACT_TYPE_LABELS[contact.contact_type]} {normalized} добавлен вручную",
            event_data={"contact_type": contact.contact_type, "value": normalized},
        )
    )
    db.commit()
    return company_to_dict(company, settings, detailed=True)


@app.delete("/api/companies/{company_id}/contacts/{contact_id}")
def delete_company_contact(
    company_id: int,
    contact_id: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    contact = next((item for item in company.contacts if item.id == contact_id), None)
    if contact is None:
        raise HTTPException(status_code=404, detail="Контакт не найден")
    if contact.source != "Вручную":
        raise HTTPException(
            status_code=422,
            detail="Контакт от провайдера обновляется автоматически и не удаляется вручную",
        )

    label = CONTACT_TYPE_LABELS[contact.contact_type]
    value = contact.value
    company.contacts.remove(contact)
    company.last_updated_at = datetime.now(timezone.utc)
    db.add(
        ActivityHistory(
            company=company,
            event_type="contact_deleted",
            description=f"{label} {value} удалён",
            event_data={"contact_type": contact.contact_type, "value": value},
        )
    )
    db.commit()
    return company_to_dict(company, settings, detailed=True)


@app.delete("/api/companies/{company_id}")
def delete_company(
    company_id: int,
    db: Session = Depends(get_db),
) -> dict:
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Компания не найдена")

    excluded = db.scalar(select(ExcludedCompany).where(ExcludedCompany.inn == company.inn))
    if excluded is None:
        db.add(ExcludedCompany(inn=company.inn, name=company.name))
    else:
        excluded.name = company.name
        excluded.deleted_at = datetime.now(timezone.utc)
    deleted = {"id": company.id, "inn": company.inn, "name": company.name}
    db.delete(company)
    db.commit()
    return {"deleted": True, "excluded_from_discovery": True, **deleted}


def _company_recipients(company: Company, requested_recipient: str | None) -> list[str]:
    recipients: list[str] = []
    seen: set[str] = set()
    for item in company.emails:
        normalized = normalize_email(item.email)
        if normalized and normalized not in seen:
            recipients.append(normalized)
            seen.add(normalized)

    recipient = normalize_email(requested_recipient or "")
    if requested_recipient and recipient not in seen:
        raise HTTPException(status_code=422, detail="Выбранный email не принадлежит компании")
    if recipient:
        return [recipient]
    if recipients:
        return [recipients[0]]
    raise HTTPException(status_code=422, detail="У компании нет email для отправки")


def _company_recipient(company: Company, requested_recipient: str | None) -> str:
    return _company_recipients(company, requested_recipient)[0]


@app.get("/api/email-template")
def get_email_template(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    return email_template_to_dict(get_or_create_email_template(db), settings)


@app.put("/api/email-template")
def update_email_template(
    update: EmailTemplateUpdate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    try:
        render_email_template(update.subject_template, {item: "" for item in ("company_name", "date", "inn", "primary_okved", "email")})
        render_email_template(update.body_template, {item: "" for item in ("company_name", "date", "inn", "primary_okved", "email")})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    template = get_or_create_email_template(db)
    template.subject_template = update.subject_template
    template.body_template = update.body_template
    template.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(template)
    return email_template_to_dict(template, settings)


@app.post("/api/email-template/preview")
def preview_email_template(
    request: EmailPreviewRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    company = db.get(Company, request.company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    recipient = _company_recipient(company, request.recipient)
    template = get_or_create_email_template(db)
    values = company_template_values(company, recipient, settings)
    try:
        subject = render_email_template(request.subject_template or template.subject_template, values)
        body = append_opt_out_footer(
            render_email_template(request.body_template or template.body_template, values),
            settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "company_id": company.id,
        "company_name": company.name,
        "recipient": recipient,
        "subject": subject,
        "body": body,
    }


@app.post("/api/companies/{company_id}/send-email")
def send_company_email(
    company_id: int,
    request: EmailSendRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Компания не найдена")
    if not settings.gmail_oauth_configured:
        raise HTTPException(status_code=503, detail="Gmail OAuth не настроен")

    recipients = _company_recipients(company, request.recipient)
    try:
        assert_manual_send_allowed(db, recipients[0], settings)
    except OutreachPolicyError as exc:
        detail = str(exc)
        if exc.retry_at:
            detail += f". После: {exc.retry_at.astimezone(settings.timezone).isoformat()}"
        raise HTTPException(status_code=429, detail=detail) from exc
    template = get_or_create_email_template(db)
    sent_messages: list[tuple[str, str, str]] = []
    try:
        config = GmailOAuthConfig(
            sender_email=settings.outreach_sender_email,
            client_id=settings.gmail_client_id,
            client_secret=settings.gmail_client_secret,
            refresh_token=settings.gmail_refresh_token,
            timeout_seconds=settings.gmail_timeout_seconds,
        )
        with GmailOAuthSender(config) as sender:
            for recipient in recipients:
                values = company_template_values(company, recipient, settings)
                subject = render_email_template(request.subject or template.subject_template, values).strip()
                body = append_opt_out_footer(
                    render_email_template(request.body or template.body_template, values),
                    settings,
                )
                message_id = sender.send(recipient, subject, body)
                sent_messages.append((recipient, message_id, subject))
    except (GmailOAuthError, ValueError) as exc:
        mark_company_send_failed(
            db,
            company.id,
            recipients[0],
            str(exc),
        )
        db.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        reason = "Неизвестная ошибка отправки"
        mark_company_send_failed(
            db,
            company.id,
            recipients[0],
            reason,
        )
        db.commit()
        raise HTTPException(status_code=502, detail=reason) from exc

    previous_status = company.status
    company.status = "sent"
    company.last_updated_at = datetime.now(timezone.utc)
    for recipient, message_id, subject in sent_messages:
        db.add(
            ActivityHistory(
                company=company,
                event_type="email_sent",
                description=f"Письмо отправлено на {recipient}",
                from_status=previous_status,
                to_status="sent",
                event_data={"recipient": recipient, "message_id": message_id, "subject": subject},
            )
        )
    db.commit()
    recipient, message_id, _ = sent_messages[0]
    return {
        "message_id": message_id,
        "message_ids": [item[1] for item in sent_messages],
        "company_id": company.id,
        "recipient": recipient,
        "recipients": [item[0] for item in sent_messages],
        "sent_count": len(sent_messages),
        "status": company.status,
        "sent_at": datetime.now(settings.timezone).isoformat(),
    }


def _campaign_or_404(db: Session, campaign_id: int):
    from app.models import OutreachCampaign

    campaign = db.get(OutreachCampaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Рассылка не найдена")
    return campaign


@app.post("/api/outreach/preflight")
def outreach_preflight(
    request: OutreachPreflightRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    return build_outreach_preflight(db, request.filters, settings)


@app.get("/api/outreach/campaigns/active")
def get_active_outreach_campaign(db: Session = Depends(get_db)) -> dict | None:
    campaign = active_outreach_campaign(db)
    return outreach_campaign_to_dict(campaign) if campaign else None


@app.post("/api/outreach/campaigns", status_code=202)
def start_outreach_campaign(
    request: OutreachCampaignCreate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    try:
        campaign = create_outreach_campaign(db, request.filters, settings)
    except OutreachPolicyError as exc:
        status_code = 503 if not settings.gmail_oauth_configured else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    wake_outreach_worker()
    return outreach_campaign_to_dict(campaign)


@app.get("/api/outreach/campaigns/{campaign_id}")
def get_outreach_campaign(
    campaign_id: int,
    db: Session = Depends(get_db),
) -> dict:
    return outreach_campaign_to_dict(_campaign_or_404(db, campaign_id))


@app.post("/api/outreach/campaigns/{campaign_id}/pause")
def pause_campaign(campaign_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        campaign = pause_outreach_campaign(db, _campaign_or_404(db, campaign_id))
    except OutreachPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    wake_outreach_worker()
    return outreach_campaign_to_dict(campaign)


@app.post("/api/outreach/campaigns/{campaign_id}/resume")
def resume_campaign(campaign_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        campaign = resume_outreach_campaign(db, _campaign_or_404(db, campaign_id))
    except OutreachPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    wake_outreach_worker()
    return outreach_campaign_to_dict(campaign)


@app.post("/api/outreach/campaigns/{campaign_id}/cancel")
def cancel_campaign(campaign_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        campaign = cancel_outreach_campaign(db, _campaign_or_404(db, campaign_id))
    except OutreachPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    wake_outreach_worker()
    return outreach_campaign_to_dict(campaign)


@app.post("/api/search-runs", status_code=202)
def start_search(
    request: SearchRunCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    running = db.scalar(select(SearchRun).where(SearchRun.status.in_(("pending", "running"))))
    if running:
        raise HTTPException(status_code=409, detail="Поиск уже выполняется")
    run = SearchRun(
        requested_okved_codes=request.okved_codes,
        mode=settings.resolved_discovery_provider,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    background_tasks.add_task(run_discovery, run.id, settings, request.limit_per_code)
    return search_run_to_dict(run)


@app.get("/api/search-runs/{run_id}")
def get_search_run(run_id: int, db: Session = Depends(get_db)) -> dict:
    run = db.get(SearchRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Запуск поиска не найден")
    return search_run_to_dict(run)


@app.get("/api/export.xlsx")
def export_companies(
    filters: CompanyFilters = Depends(filters_dependency),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    companies = list(
        db.scalars(
            build_company_query(filters, settings.timezone).order_by(Company.first_discovered_at.desc())
        ).all()
    )
    content = build_xlsx(companies, settings)
    filename = f"fuellead-{date.today().isoformat()}.xlsx"
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
