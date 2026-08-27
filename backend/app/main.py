from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from io import BytesIO

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import DEFAULT_OKVED_CODES, Settings, get_settings
from app.database import SessionLocal, create_database, get_db
from app.export import build_xlsx
from app.models import ActivityHistory, Company, MVP_STATUSES, SearchRun
from app.queries import build_company_query
from app.schemas import CompanyFilters, SearchRunCreate, StatusUpdate
from app.serializers import company_to_dict, search_run_to_dict
from app.services.discovery import fail_interrupted_search_runs, run_discovery


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_database()
    with SessionLocal() as db:
        fail_interrupted_search_runs(db)
    yield


app = FastAPI(title="FuelLead API", version="0.1.0", lifespan=lifespan)
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
        "mode": "checko" if settings.checko_configured else "demo",
        "default_okved_codes": DEFAULT_OKVED_CODES,
        "discovery_limit_per_code": settings.discovery_limit_per_code,
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
    if update.status not in MVP_STATUSES:
        raise HTTPException(status_code=422, detail="Недоступный статус MVP")
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
