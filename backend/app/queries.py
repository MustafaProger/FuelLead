from datetime import datetime, time, timedelta, timezone, tzinfo

from sqlalchemy import Select, or_, select

from app.email_providers import email_provider_predicate
from app.models import Company
from app.schemas import CompanyFilters


def build_company_query(
    filters: CompanyFilters, local_timezone: tzinfo = timezone.utc
) -> Select[tuple[Company]]:
    query = select(Company)
    if filters.status:
        query = query.where(Company.status == filters.status)
    if filters.has_email is True:
        query = query.where(Company.emails.any())
    elif filters.has_email is False:
        query = query.where(~Company.emails.any())
    if filters.email_provider:
        query = query.where(Company.emails.any(email_provider_predicate(filters.email_provider)))
    if filters.category:
        query = query.where(Company.activity_category == filters.category)
    if filters.discovered_on:
        local_start = datetime.combine(filters.discovered_on, time.min, tzinfo=local_timezone)
        start = local_start.astimezone(timezone.utc)
        end = (local_start + timedelta(days=1)).astimezone(timezone.utc)
        query = query.where(Company.first_discovered_at >= start, Company.first_discovered_at < end)
    if filters.search:
        needle = f"%{filters.search.strip()}%"
        query = query.where(or_(Company.name.ilike(needle), Company.inn.ilike(needle)))
    return query
