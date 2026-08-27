from contextlib import asynccontextmanager
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import DEFAULT_OKVED_CODES, TARGET_REGION_CODES, Settings, get_settings
from app.database import SessionLocal, create_database, get_db
from app.export import build_xlsx
from app.models import ALL_STATUSES, ActivityHistory, Company, SearchRun
from app.queries import build_company_query
from app.schemas import (
    CompanyFilters,
    EmailPreviewRequest,
    EmailSendRequest,
    EmailTemplateUpdate,
    SearchRunCreate,
    StatusUpdate,
)
from app.serializers import as_aware, company_to_dict, search_run_to_dict
from app.services.checko import normalize_email
from app.services.discovery import fail_interrupted_search_runs, run_discovery, sanitize_search_run_errors
from app.services.email_templates import (
    company_template_values,
    email_template_to_dict,
    get_or_create_email_template,
    render_email_template,
)
from app.services.gmail import GmailOAuthConfig, GmailOAuthError, GmailOAuthSender


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_database()
    with SessionLocal() as db:
        fail_interrupted_search_runs(db)
        sanitize_search_run_errors(db)
    yield


app = FastAPI(title="FuelLead API", version="0.1.0", lifespan=lifespan)
DASHBOARD_HISTORY_DAYS = 183
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def filters_dependency(
    status: str | None = None,
    has_email: bool | None = None,
    category: str | None = None,
    discovered_on: date | None = None,
    search: str | None = None,
) -> CompanyFilters:
    return CompanyFilters(
        status=status,
        has_email=has_email,
        category=category,
        discovered_on=discovered_on,
        search=search,
    )


@app.get("/api/health")
def health(settings: Settings = Depends(get_settings)) -> dict:
    return {
        "status": "ok",
        "app": settings.app_name,
        "checko_configured": settings.checko_configured,
        "checko_api_key_count": len(settings.checko_api_keys),
        "mode": "checko" if settings.checko_configured else "demo",
        "default_okved_codes": DEFAULT_OKVED_CODES,
        "target_region_codes": TARGET_REGION_CODES,
        "discovery_limit_per_code": settings.discovery_limit_per_code,
        "outreach_sender_email": settings.outreach_sender_email,
        "gmail_auth_mode": "oauth2",
        "gmail_oauth_configured": settings.gmail_oauth_configured,
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
    first_day = local_today - timedelta(days=DASHBOARD_HISTORY_DAYS - 1)
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
            "ready": status_counts["ready"],
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
                description=f"Статус изменён: {previous} → {update.status}",
                from_status=previous,
                to_status=update.status,
            )
        )
        db.commit()
    return company_to_dict(company, settings, detailed=True)


def _company_recipient(company: Company, requested_recipient: str | None) -> str:
    recipient = normalize_email(requested_recipient or "")
    company_emails = {normalize_email(item.email) for item in company.emails}
    if requested_recipient and recipient not in company_emails:
        raise HTTPException(status_code=422, detail="Выбранный email не принадлежит компании")
    if recipient:
        return recipient
    if company.emails:
        return normalize_email(company.emails[0].email)
    raise HTTPException(status_code=422, detail="У компании нет email для отправки")


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
        body = render_email_template(request.body_template or template.body_template, values)
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

    recipient = _company_recipient(company, request.recipient)
    template = get_or_create_email_template(db)
    values = company_template_values(company, recipient, settings)
    try:
        subject = render_email_template(request.subject or template.subject_template, values).strip()
        body = render_email_template(request.body or template.body_template, values).strip()
        config = GmailOAuthConfig(
            sender_email=settings.outreach_sender_email,
            client_id=settings.gmail_client_id,
            client_secret=settings.gmail_client_secret,
            refresh_token=settings.gmail_refresh_token,
            timeout_seconds=settings.gmail_timeout_seconds,
        )
        with GmailOAuthSender(config) as sender:
            message_id = sender.send(recipient, subject, body)
    except (GmailOAuthError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    previous_status = company.status
    company.status = "sent"
    company.last_updated_at = datetime.now(timezone.utc)
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
    return {
        "message_id": message_id,
        "company_id": company.id,
        "recipient": recipient,
        "status": company.status,
        "sent_at": datetime.now(settings.timezone).isoformat(),
    }


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
        mode="checko" if settings.checko_configured else "demo",
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
