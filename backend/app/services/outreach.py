import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import SessionLocal
from app.models import (
    ActivityHistory,
    Company,
    OutreachCampaign,
    OutreachDelivery,
)
from app.queries import build_company_query
from app.schemas import CompanyFilters
from app.services.email_templates import (
    company_template_values,
    get_or_create_email_template,
    render_email_template,
)
from app.services.gmail import GmailOAuthConfig, GmailOAuthError, GmailOAuthSender
from app.services.provider import normalize_email


ACTIVE_CAMPAIGN_STATUSES = ("running", "paused")


class OutreachPolicyError(RuntimeError):
    def __init__(self, message: str, *, retry_at: datetime | None = None):
        super().__init__(message)
        self.retry_at = retry_at


@dataclass(frozen=True, slots=True)
class OutreachCandidate:
    company_id: int
    company_name: str
    company_inn: str
    recipient: str
    recipient_domain: str
    subject: str
    body: str


@dataclass(frozen=True, slots=True)
class OutreachSelection:
    matched_count: int
    eligible_count: int
    candidates: tuple[OutreachCandidate, ...]
    skipped: dict[str, int]


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    aware = _aware(value)
    return aware.isoformat() if aware else None


def mark_company_send_failed(
    db: Session,
    company_id: int | None,
    recipient: str,
    reason: str,
    *,
    campaign_id: int | None = None,
    occurred_at: datetime | None = None,
) -> Company | None:
    """Expose a delivery failure on the company and preserve its reason in history."""
    company = db.get(Company, company_id) if company_id else None
    if company is None:
        return None
    timestamp = _aware(occurred_at) or datetime.now(timezone.utc)
    previous_status = company.status
    company.status = "error"
    company.last_updated_at = timestamp
    event_data: dict[str, object] = {"recipient": recipient, "error": reason}
    if campaign_id is not None:
        event_data["campaign_id"] = campaign_id
    db.add(
        ActivityHistory(
            company=company,
            event_type="email_failed",
            description=f"Ошибка отправки на {recipient}: {reason}",
            from_status=previous_status,
            to_status="error",
            event_data=event_data,
            created_at=timestamp,
        )
    )
    return company


def append_opt_out_footer(body: str, settings: Settings) -> str:
    footer = settings.outreach_opt_out_text.strip()
    clean_body = body.strip()
    if not footer or footer in clean_body:
        return clean_body
    return f"{clean_body}\n\n—\n{footer}"


def _previously_contacted_addresses(db: Session) -> set[str]:
    addresses = {
        recipient
        for recipient in db.scalars(
            select(OutreachDelivery.recipient).where(OutreachDelivery.status == "sent")
        ).all()
        if recipient
    }
    for event_data in db.scalars(
        select(ActivityHistory.event_data).where(ActivityHistory.event_type == "email_sent")
    ).all():
        if not isinstance(event_data, dict):
            continue
        recipient = normalize_email(str(event_data.get("recipient") or ""))
        if recipient:
            addresses.add(recipient)
    return addresses


def select_outreach_candidates(
    db: Session,
    filters: CompanyFilters,
    settings: Settings,
) -> OutreachSelection:
    companies = list(
        db.scalars(
            build_company_query(filters, settings.timezone).order_by(
                Company.first_discovered_at.asc(), Company.id.asc()
            )
        ).all()
    )
    template = get_or_create_email_template(db)
    contacted = _previously_contacted_addresses(db)
    selected_addresses: set[str] = set()
    candidates: list[OutreachCandidate] = []
    skipped = Counter(
        {
            "not_new": 0,
            "inactive": 0,
            "without_email": 0,
            "already_contacted": 0,
            "duplicate_address": 0,
        }
    )

    for company in companies:
        is_not_new = company.status != "new"
        is_inactive = not company.is_active
        if is_not_new:
            skipped["not_new"] += 1
        if is_inactive:
            skipped["inactive"] += 1
        recipients = [
            normalized
            for email in company.emails
            if (normalized := normalize_email(email.email))
        ]
        recipients = list(dict.fromkeys(recipients))
        if not recipients:
            skipped["without_email"] += 1
        recipient = recipients[0] if recipients else None
        was_contacted = bool(recipient and recipient in contacted)
        if was_contacted:
            skipped["already_contacted"] += 1

        # Diagnostic counters intentionally overlap. This keeps the preflight
        # consistent with the dashboard (for example, a new company without an
        # email is counted in both groups) instead of hiding later reasons
        # behind the first failed check.
        if is_not_new or is_inactive or not recipient or was_contacted:
            continue

        # One company gets one message. We intentionally do not fall back to a
        # second inbox when the primary address was already contacted.
        if recipient in selected_addresses:
            skipped["duplicate_address"] += 1
            continue

        values = company_template_values(company, recipient, settings)
        subject = render_email_template(template.subject_template, values).strip()
        body = append_opt_out_footer(
            render_email_template(template.body_template, values),
            settings,
        )
        selected_addresses.add(recipient)
        candidates.append(
            OutreachCandidate(
                company_id=company.id,
                company_name=company.name,
                company_inn=company.inn,
                recipient=recipient,
                recipient_domain=recipient.rsplit("@", 1)[1],
                subject=subject,
                body=body,
            )
        )

    return OutreachSelection(
        matched_count=len(companies),
        eligible_count=len(candidates),
        candidates=tuple(candidates[: settings.outreach_batch_limit]),
        skipped=dict(skipped),
    )


def outreach_policy_dict(settings: Settings) -> dict:
    return {
        "campaign_limit": settings.outreach_batch_limit,
        "daily_limit": settings.outreach_daily_limit,
        "hourly_limit": settings.outreach_hourly_limit,
        "min_interval_seconds": settings.outreach_min_interval_seconds,
        "max_per_domain_per_day": settings.outreach_max_per_domain_per_day,
        "eligible_status": "new",
        "primary_address_only": True,
        "automatic_stop_on_provider_error": True,
        "automatic_send_enabled": settings.outreach_automatic_send_enabled,
        "opt_out_footer_enabled": bool(settings.outreach_opt_out_text.strip()),
    }


def build_outreach_preflight(
    db: Session,
    filters: CompanyFilters,
    settings: Settings,
) -> dict:
    selection = select_outreach_candidates(db, filters, settings)
    sample = selection.candidates[0] if selection.candidates else None
    return {
        "matched_count": selection.matched_count,
        "eligible_count": selection.eligible_count,
        "selected_count": len(selection.candidates),
        "deferred_by_campaign_limit": max(
            0, selection.eligible_count - len(selection.candidates)
        ),
        "skipped": selection.skipped,
        "sender_email": settings.outreach_sender_email,
        "gmail_configured": settings.gmail_oauth_configured,
        "policy": outreach_policy_dict(settings),
        "sample": {
            "company_name": sample.company_name,
            "recipient": sample.recipient,
            "subject": sample.subject,
            "body": sample.body,
        }
        if sample
        else None,
    }


def active_outreach_campaign(db: Session) -> OutreachCampaign | None:
    return db.scalar(
        select(OutreachCampaign)
        .where(OutreachCampaign.status.in_(ACTIVE_CAMPAIGN_STATUSES))
        .order_by(OutreachCampaign.id.desc())
    )


def create_outreach_campaign(
    db: Session,
    filters: CompanyFilters,
    settings: Settings,
) -> OutreachCampaign:
    if not settings.gmail_oauth_configured:
        raise OutreachPolicyError("Gmail OAuth не настроен")
    if active_outreach_campaign(db):
        raise OutreachPolicyError("Другая рассылка уже выполняется или стоит на паузе")

    selection = select_outreach_candidates(db, filters, settings)
    if not selection.candidates:
        raise OutreachPolicyError(
            "Нет получателей: нужны действующие компании со статусом «Новая», "
            "которым ещё не отправляли письмо"
        )

    campaign = OutreachCampaign(
        status="running",
        filters=filters.model_dump(mode="json"),
        matched_count=selection.matched_count,
        recipient_count=len(selection.candidates),
        daily_limit=settings.outreach_daily_limit,
        hourly_limit=settings.outreach_hourly_limit,
        min_interval_seconds=settings.outreach_min_interval_seconds,
        max_per_domain_per_day=settings.outreach_max_per_domain_per_day,
        next_send_at=datetime.now(timezone.utc),
    )
    campaign.deliveries.extend(
        OutreachDelivery(
            company_id=candidate.company_id,
            company_name=candidate.company_name,
            company_inn=candidate.company_inn,
            recipient=candidate.recipient,
            recipient_domain=candidate.recipient_domain,
            subject=candidate.subject,
            body=candidate.body,
        )
        for candidate in selection.candidates
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return campaign


def outreach_campaign_to_dict(campaign: OutreachCampaign) -> dict:
    processed = campaign.sent_count + campaign.failed_count + campaign.cancelled_count
    remaining = max(0, campaign.recipient_count - processed)
    return {
        "id": campaign.id,
        "status": campaign.status,
        "matched_count": campaign.matched_count,
        "recipient_count": campaign.recipient_count,
        "sent_count": campaign.sent_count,
        "failed_count": campaign.failed_count,
        "cancelled_count": campaign.cancelled_count,
        "remaining_count": remaining,
        "progress_percent": round(
            (processed / campaign.recipient_count) * 100 if campaign.recipient_count else 0,
            1,
        ),
        "pause_reason": campaign.pause_reason,
        "next_send_at": _iso(campaign.next_send_at),
        "last_sent_at": _iso(campaign.last_sent_at),
        "started_at": _iso(campaign.started_at),
        "completed_at": _iso(campaign.completed_at),
        "created_at": _iso(campaign.created_at),
        "policy": {
            "daily_limit": campaign.daily_limit,
            "hourly_limit": campaign.hourly_limit,
            "min_interval_seconds": campaign.min_interval_seconds,
            "max_per_domain_per_day": campaign.max_per_domain_per_day,
        },
    }


def pause_outreach_campaign(db: Session, campaign: OutreachCampaign) -> OutreachCampaign:
    if campaign.status != "running":
        raise OutreachPolicyError("На паузу можно поставить только активную рассылку")
    campaign.status = "paused"
    campaign.pause_reason = "Остановлено пользователем"
    db.commit()
    return campaign


def resume_outreach_campaign(db: Session, campaign: OutreachCampaign) -> OutreachCampaign:
    if campaign.status != "paused":
        raise OutreachPolicyError("Продолжить можно только рассылку на паузе")
    if active := active_outreach_campaign(db):
        if active.id != campaign.id:
            raise OutreachPolicyError("Другая рассылка уже активна")
    campaign.status = "running"
    campaign.pause_reason = None
    campaign.next_send_at = datetime.now(timezone.utc)
    db.commit()
    return campaign


def cancel_outreach_campaign(db: Session, campaign: OutreachCampaign) -> OutreachCampaign:
    if campaign.status not in ACTIVE_CAMPAIGN_STATUSES:
        raise OutreachPolicyError("Эта рассылка уже завершена")
    queued = list(
        db.scalars(
            select(OutreachDelivery).where(
                OutreachDelivery.campaign_id == campaign.id,
                OutreachDelivery.status == "queued",
            )
        ).all()
    )
    for delivery in queued:
        delivery.status = "cancelled"
    campaign.cancelled_count += len(queued)
    campaign.status = "cancelled"
    campaign.pause_reason = "Отменено пользователем"
    campaign.completed_at = datetime.now(timezone.utc)
    campaign.next_send_at = None
    db.commit()
    return campaign


def recover_interrupted_outreach(db: Session) -> None:
    interrupted = list(
        db.scalars(
            select(OutreachDelivery).where(OutreachDelivery.status == "sending")
        ).all()
    )
    for delivery in interrupted:
        delivery.status = "failed"
        delivery.error_message = (
            "Результат отправки неизвестен после перезапуска; автоматический повтор отключён"
        )
        campaign = delivery.campaign
        campaign.failed_count += 1
        campaign.status = "paused"
        campaign.pause_reason = (
            "Backend был перезапущен во время отправки. Проверьте Gmail перед продолжением"
        )
        campaign.next_send_at = None
        mark_company_send_failed(
            db,
            delivery.company_id,
            delivery.recipient,
            delivery.error_message,
            campaign_id=campaign.id,
        )
    if interrupted:
        db.commit()


def _local_day_bounds(now: datetime, settings: Settings) -> tuple[datetime, datetime]:
    local_now = now.astimezone(settings.timezone)
    local_start = datetime.combine(local_now.date(), time.min, tzinfo=settings.timezone)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _sent_records(db: Session) -> list[tuple[datetime, str]]:
    records: list[tuple[datetime, str]] = []
    for sent_at, domain in db.execute(
        select(OutreachDelivery.sent_at, OutreachDelivery.recipient_domain).where(
            OutreachDelivery.status == "sent",
            OutreachDelivery.sent_at.is_not(None),
        )
    ):
        aware = _aware(sent_at)
        if aware:
            records.append((aware, domain))

    for created_at, event_data in db.execute(
        select(ActivityHistory.created_at, ActivityHistory.event_data).where(
            ActivityHistory.event_type == "email_sent"
        )
    ):
        if not isinstance(event_data, dict) or event_data.get("campaign_id"):
            continue
        recipient = normalize_email(str(event_data.get("recipient") or ""))
        aware = _aware(created_at)
        if recipient and aware:
            records.append((aware, recipient.rsplit("@", 1)[1]))
    return records


def _global_policy_retry_at(
    records: list[tuple[datetime, str]],
    now: datetime,
    settings: Settings,
    *,
    daily_limit: int,
    hourly_limit: int,
    min_interval_seconds: int,
) -> datetime | None:
    day_start, next_day = _local_day_bounds(now, settings)
    today_records = sorted(sent_at for sent_at, _ in records if sent_at >= day_start)
    if len(today_records) >= daily_limit:
        return next_day

    retry_candidates: list[datetime] = []
    if hourly_limit > 0:
        hour_start = now - timedelta(hours=1)
        hour_records = sorted(sent_at for sent_at, _ in records if sent_at > hour_start)
        if len(hour_records) >= hourly_limit:
            retry_candidates.append(hour_records[-hourly_limit] + timedelta(hours=1))
    if records:
        latest = max(sent_at for sent_at, _ in records)
        interval_retry = latest + timedelta(seconds=min_interval_seconds)
        if interval_retry > now:
            retry_candidates.append(interval_retry)
    return max(retry_candidates) if retry_candidates else None


def assert_manual_send_allowed(
    db: Session,
    recipient: str,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> None:
    if active_outreach_campaign(db):
        raise OutreachPolicyError(
            "Одиночная отправка недоступна, пока массовая рассылка активна или стоит на паузе"
        )
    effective_now = _aware(now) or datetime.now(timezone.utc)
    records = _sent_records(db)
    retry_at = _global_policy_retry_at(
        records,
        effective_now,
        settings,
        daily_limit=settings.outreach_daily_limit,
        hourly_limit=settings.outreach_hourly_limit,
        min_interval_seconds=settings.outreach_min_interval_seconds,
    )
    if retry_at and retry_at > effective_now:
        raise OutreachPolicyError(
            "Безопасный лимит отправки достигнут. Повторите после указанного времени",
            retry_at=retry_at,
        )
    domain = recipient.rsplit("@", 1)[1]
    day_start, next_day = _local_day_bounds(effective_now, settings)
    domain_count = sum(
        1 for sent_at, sent_domain in records if sent_at >= day_start and sent_domain == domain
    )
    if domain_count >= settings.outreach_max_per_domain_per_day:
        raise OutreachPolicyError(
            "Дневной лимит для домена получателя достигнут",
            retry_at=next_day,
        )


def process_outreach_tick(
    settings: Settings,
    *,
    sender_factory: Callable[..., GmailOAuthSender] = GmailOAuthSender,
    session_factory: Callable[[], Session] | None = None,
    now: datetime | None = None,
) -> float:
    effective_now = _aware(now) or datetime.now(timezone.utc)
    with (session_factory or SessionLocal)() as db:
        campaign = db.scalar(
            select(OutreachCampaign)
            .where(OutreachCampaign.status == "running")
            .order_by(OutreachCampaign.id.asc())
        )
        if campaign is None:
            if not (
                settings.outreach_automatic_send_enabled
                and settings.gmail_oauth_configured
            ):
                return float(settings.outreach_worker_poll_seconds)
            try:
                # Newly discovered companies enter the automatic queue without
                # requiring the dialog to stay open or another manual click.
                campaign = create_outreach_campaign(db, CompanyFilters(), settings)
                campaign.next_send_at = effective_now
                db.commit()
            except OutreachPolicyError:
                return float(settings.outreach_worker_poll_seconds)

        scheduled_at = _aware(campaign.next_send_at)
        if scheduled_at and scheduled_at > effective_now:
            return max(1.0, (scheduled_at - effective_now).total_seconds())

        records = _sent_records(db)
        retry_at = _global_policy_retry_at(
            records,
            effective_now,
            settings,
            daily_limit=campaign.daily_limit,
            hourly_limit=campaign.hourly_limit,
            min_interval_seconds=campaign.min_interval_seconds,
        )
        if retry_at and retry_at > effective_now:
            campaign.next_send_at = retry_at
            db.commit()
            return max(1.0, (retry_at - effective_now).total_seconds())

        queued = list(
            db.scalars(
                select(OutreachDelivery)
                .where(
                    OutreachDelivery.campaign_id == campaign.id,
                    OutreachDelivery.status == "queued",
                )
                .order_by(OutreachDelivery.id.asc())
            ).all()
        )
        contacted = _previously_contacted_addresses(db)
        eligible_queued: list[OutreachDelivery] = []
        for item in queued:
            company = db.get(Company, item.company_id) if item.company_id else None
            if (
                company is not None
                and company.is_active
                and company.status == "new"
                and item.recipient not in contacted
            ):
                eligible_queued.append(item)
                continue
            item.status = "cancelled"
            item.error_message = (
                "Пропущено перед отправкой: компания больше не соответствует условиям"
            )
            campaign.cancelled_count += 1
        queued = eligible_queued
        if not queued:
            campaign.status = "completed"
            campaign.completed_at = effective_now
            campaign.next_send_at = None
            db.commit()
            return float(settings.outreach_worker_poll_seconds)

        day_start, next_day = _local_day_bounds(effective_now, settings)
        domain_counts = Counter(
            domain for sent_at, domain in records if sent_at >= day_start
        )
        delivery = next(
            (
                item
                for item in queued
                if domain_counts[item.recipient_domain] < campaign.max_per_domain_per_day
            ),
            None,
        )
        if delivery is None:
            campaign.next_send_at = next_day
            db.commit()
            return max(1.0, (next_day - effective_now).total_seconds())

        delivery.status = "sending"
        delivery.started_at = effective_now
        delivery.error_message = None
        campaign.next_send_at = None
        delivery_id = delivery.id
        campaign_id = campaign.id
        recipient = delivery.recipient
        subject = delivery.subject
        body = delivery.body
        db.commit()

        try:
            config = GmailOAuthConfig(
                sender_email=settings.outreach_sender_email,
                client_id=settings.gmail_client_id,
                client_secret=settings.gmail_client_secret,
                refresh_token=settings.gmail_refresh_token,
                timeout_seconds=settings.gmail_timeout_seconds,
            )
            with sender_factory(config) as sender:
                message_id = sender.send(recipient, subject, body)
        except (GmailOAuthError, ValueError) as exc:
            db.expire_all()
            delivery = db.get(OutreachDelivery, delivery_id)
            campaign = db.get(OutreachCampaign, campaign_id)
            if delivery and campaign:
                delivery.status = "failed"
                delivery.error_message = str(exc)
                campaign.failed_count += 1
                if campaign.status != "cancelled":
                    campaign.status = "paused"
                    campaign.pause_reason = (
                        f"Отправка остановлена после ошибки Gmail: {exc}"
                    )
                campaign.next_send_at = None
                mark_company_send_failed(
                    db,
                    delivery.company_id,
                    delivery.recipient,
                    delivery.error_message,
                    campaign_id=campaign.id,
                    occurred_at=effective_now,
                )
                db.commit()
            return float(settings.outreach_worker_poll_seconds)
        except Exception:
            db.expire_all()
            delivery = db.get(OutreachDelivery, delivery_id)
            campaign = db.get(OutreachCampaign, campaign_id)
            if delivery and campaign:
                delivery.status = "failed"
                delivery.error_message = "Неизвестная ошибка отправки"
                campaign.failed_count += 1
                if campaign.status != "cancelled":
                    campaign.status = "paused"
                    campaign.pause_reason = (
                        "Отправка остановлена после неизвестной ошибки. Проверьте Gmail"
                    )
                campaign.next_send_at = None
                mark_company_send_failed(
                    db,
                    delivery.company_id,
                    delivery.recipient,
                    delivery.error_message,
                    campaign_id=campaign.id,
                    occurred_at=effective_now,
                )
                db.commit()
            return float(settings.outreach_worker_poll_seconds)

        sent_at = effective_now if now is not None else datetime.now(timezone.utc)
        db.expire_all()
        delivery = db.get(OutreachDelivery, delivery_id)
        campaign = db.get(OutreachCampaign, campaign_id)
        if delivery is None or campaign is None:
            return float(settings.outreach_worker_poll_seconds)
        delivery.status = "sent"
        delivery.message_id = message_id
        delivery.sent_at = sent_at
        campaign.sent_count += 1
        campaign.last_sent_at = sent_at

        company = db.get(Company, delivery.company_id) if delivery.company_id else None
        if company is not None:
            previous_status = company.status
            company.status = "sent"
            company.last_updated_at = sent_at
            db.add(
                ActivityHistory(
                    company=company,
                    event_type="email_sent",
                    description=f"Письмо отправлено на {delivery.recipient}",
                    from_status=previous_status,
                    to_status="sent",
                    event_data={
                        "recipient": delivery.recipient,
                        "message_id": message_id,
                        "subject": delivery.subject,
                        "campaign_id": campaign.id,
                    },
                )
            )

        remaining = db.scalar(
            select(func.count(OutreachDelivery.id)).where(
                OutreachDelivery.campaign_id == campaign.id,
                OutreachDelivery.status == "queued",
            )
        ) or 0
        if campaign.status == "running":
            if remaining:
                campaign.next_send_at = sent_at + timedelta(
                    seconds=campaign.min_interval_seconds
                )
            else:
                campaign.status = "completed"
                campaign.completed_at = sent_at
                campaign.next_send_at = None
        else:
            campaign.next_send_at = None
        db.commit()
        return (
            float(campaign.min_interval_seconds)
            if campaign.status == "running"
            else float(settings.outreach_worker_poll_seconds)
        )


_worker_loop: asyncio.AbstractEventLoop | None = None
_worker_wakeup: asyncio.Event | None = None


def wake_outreach_worker() -> None:
    if _worker_loop and _worker_wakeup:
        _worker_loop.call_soon_threadsafe(_worker_wakeup.set)


async def run_outreach_worker(settings: Settings) -> None:
    global _worker_loop, _worker_wakeup
    _worker_loop = asyncio.get_running_loop()
    _worker_wakeup = asyncio.Event()
    try:
        while True:
            try:
                delay = await asyncio.to_thread(process_outreach_tick, settings)
            except Exception:
                delay = float(settings.outreach_worker_poll_seconds)
            timeout = min(max(1.0, delay), float(settings.outreach_worker_poll_seconds))
            try:
                await asyncio.wait_for(_worker_wakeup.wait(), timeout=timeout)
                _worker_wakeup.clear()
            except TimeoutError:
                pass
    finally:
        _worker_loop = None
        _worker_wakeup = None
