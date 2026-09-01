from datetime import datetime, timezone

from app.config import Settings
from app.models import Company, SearchRun
from app.services.contacts import contact_href
from app.services.provider import redact_sensitive_url


def as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def company_to_dict(company: Company, settings: Settings, *, detailed: bool = False) -> dict:
    payload = {
        "id": company.id,
        "name": company.name,
        "inn": company.inn,
        "ogrn": company.ogrn,
        "primary_okved": {
            "code": company.primary_okved_code,
            "name": company.primary_okved_name,
        },
        "activity_category": company.activity_category,
        "is_active": company.is_active,
        "status": company.status,
        "emails": [
            {
                "id": email.id,
                "email": email.email,
                "source": email.source,
            }
            for email in company.emails
        ],
        "contacts": [
            {
                "id": contact.id,
                "contact_type": contact.contact_type,
                "value": contact.value,
                "source": contact.source,
                "href": contact_href(contact.contact_type, contact.value),
            }
            for contact in sorted(company.contacts, key=lambda item: (item.contact_type, item.id))
        ],
        "first_discovered_at": as_aware(company.first_discovered_at).astimezone(settings.timezone).isoformat(),
        "last_checked_at": as_aware(company.last_checked_at).astimezone(settings.timezone).isoformat(),
        "last_updated_at": as_aware(company.last_updated_at).astimezone(settings.timezone).isoformat(),
    }
    if detailed:
        payload["additional_okveds"] = [
            {"code": item.code, "name": item.name}
            for item in sorted(company.additional_okveds, key=lambda item: item.code)
        ]
        payload["history"] = [
            {
                "id": event.id,
                "event_type": event.event_type,
                "description": event.description,
                "from_status": event.from_status,
                "to_status": event.to_status,
                "created_at": as_aware(event.created_at).astimezone(settings.timezone).isoformat(),
            }
            for event in sorted(
                company.history,
                key=lambda item: (as_aware(item.created_at), item.id or 0),
                reverse=True,
            )
        ]
    return payload


def search_run_to_dict(run: SearchRun) -> dict:
    return {
        "id": run.id,
        "status": run.status,
        "mode": run.mode,
        "requested_okved_codes": run.requested_okved_codes,
        "candidates_found": run.candidates_found,
        "companies_created": run.companies_created,
        "companies_updated": run.companies_updated,
        "skipped_inactive": run.skipped_inactive,
        "errors_count": run.errors_count,
        "error_message": redact_sensitive_url(run.error_message) if run.error_message else None,
        "created_at": run.created_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }
