import asyncio
import logging
import random
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import SessionLocal
from app.models import (
    ActivityHistory,
    Company,
    EmailSuppression,
    OutreachCampaign,
    OutreachDelivery,
    SenderAccount,
)
from app.queries import build_company_query
from app.schemas import CompanyFilters
from app.services.credentials import CredentialCipher, CredentialEncryptionError
from app.services.email_templates import company_template_values, get_or_create_email_template, render_email_template
from app.services.provider import normalize_email
from app.services.sender_accounts import batch_size_for_successes
from app.services.smtp import MailruSMTPClient, SMTPAccepted, SMTPDeliveryError
from app.services.suppressions import active_suppressed_addresses


logger = logging.getLogger("fuellead.outreach")
ACTIVE_CAMPAIGN_STATUSES = ("running", "paused", "cooldown", "interrupted")
TERMINAL_DELIVERY_STATUSES = ("accepted", "failed", "bounced", "uncertain", "suppressed", "cancelled")


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


def append_opt_out_footer(body: str, settings: Settings) -> str:
    footer = settings.outreach_opt_out_text.strip()
    clean_body = body.strip()
    if not footer or footer in clean_body:
        return clean_body
    return f"{clean_body}\n\n—\n{footer}"


def mark_company_send_failed(db: Session, company_id: int | None, recipient: str, reason: str, *, campaign_id: int | None = None, occurred_at: datetime | None = None) -> Company | None:
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
    db.add(ActivityHistory(company=company, event_type="email_failed", description=f"Ошибка отправки на {recipient}: {reason}", from_status=previous_status, to_status="error", event_data=event_data, created_at=timestamp))
    return company


def _previously_contacted_addresses(db: Session) -> set[str]:
    addresses = {normalize_email(recipient) for recipient in db.scalars(select(OutreachDelivery.recipient).where(OutreachDelivery.status.in_(("accepted", "bounced", "uncertain")))).all() if recipient}
    for event_data in db.scalars(select(ActivityHistory.event_data).where(ActivityHistory.event_type == "email_sent")).all():
        if isinstance(event_data, dict):
            if recipient := normalize_email(str(event_data.get("recipient") or "")):
                addresses.add(recipient)
    return addresses


def _verified_senders(db: Session) -> list[SenderAccount]:
    return list(db.scalars(select(SenderAccount).where(SenderAccount.provider == "mailru_smtp", SenderAccount.smtp_enabled.is_(True), SenderAccount.is_active.is_(True), SenderAccount.verification_status == "verified", SenderAccount.encrypted_password.is_not(None)).order_by(SenderAccount.id.asc())).all())


def select_outreach_candidates(db: Session, filters: CompanyFilters, settings: Settings) -> OutreachSelection:
    companies = list(db.scalars(build_company_query(filters, settings.timezone).order_by(Company.first_discovered_at.asc(), Company.id.asc())).all())
    template = get_or_create_email_template(db)
    contacted = _previously_contacted_addresses(db)
    suppressed = active_suppressed_addresses(db)
    selected_addresses: set[str] = set()
    candidates: list[OutreachCandidate] = []
    skipped = Counter({"not_new": 0, "inactive": 0, "without_email": 0, "already_contacted": 0, "duplicate_address": 0, "suppressed": 0})
    for company in companies:
        is_not_new = company.status != "new"
        is_inactive = not company.is_active
        recipients = list(dict.fromkeys(normalized for email in company.emails if (normalized := normalize_email(email.email))))
        recipient = recipients[0] if recipients else None
        was_contacted = bool(recipient and recipient in contacted)
        was_suppressed = bool(recipient and recipient in suppressed)
        if is_not_new:
            skipped["not_new"] += 1
        if is_inactive:
            skipped["inactive"] += 1
        if not recipient:
            skipped["without_email"] += 1
        if was_contacted:
            skipped["already_contacted"] += 1
        if was_suppressed:
            skipped["suppressed"] += 1
        if is_not_new or is_inactive or not recipient or was_contacted or was_suppressed:
            continue
        if recipient in selected_addresses:
            skipped["duplicate_address"] += 1
            continue
        values = company_template_values(company, recipient, settings)
        subject = render_email_template(template.subject_template, values).strip()
        body = append_opt_out_footer(render_email_template(template.body_template, values), settings)
        selected_addresses.add(recipient)
        candidates.append(OutreachCandidate(company.id, company.name, company.inn, recipient, recipient.rsplit("@", 1)[1], subject, body))
    return OutreachSelection(len(companies), len(candidates), tuple(candidates[: settings.outreach_batch_limit]), dict(skipped))


def outreach_policy_dict(settings: Settings) -> dict:
    return {
        "campaign_limit": settings.outreach_batch_limit,
        "message_interval_seconds": [settings.outreach_message_interval_min_seconds, settings.outreach_message_interval_max_seconds],
        "round_rest_minutes": [settings.outreach_round_rest_min_minutes, settings.outreach_round_rest_max_minutes],
        "snapshot_ttl_seconds": settings.outreach_snapshot_ttl_seconds,
        "eligible_status": "new",
        "primary_address_only": True,
        "sequential_smtp": True,
        "automatic_send_enabled": settings.outreach_automatic_send_enabled,
        "accepted_is_not_delivered": True,
        "opt_out_footer_enabled": bool(settings.outreach_opt_out_text.strip()),
    }


def _expire_drafts(db: Session, now: datetime) -> None:
    drafts = list(db.scalars(select(OutreachCampaign).where(OutreachCampaign.status == "draft", OutreachCampaign.snapshot_expires_at < now)).all())
    for draft in drafts:
        draft.status = "stopped"
        draft.pause_reason = "Срок подтверждения снимка истёк"
        draft.completed_at = now
        for delivery in draft.deliveries:
            if delivery.status == "queued":
                delivery.status = "cancelled"
                draft.cancelled_count += 1
    if drafts:
        db.commit()


def build_outreach_preflight(db: Session, filters: CompanyFilters, settings: Settings) -> dict:
    now = datetime.now(timezone.utc)
    _expire_drafts(db, now)
    selection = select_outreach_candidates(db, filters, settings)
    senders = _verified_senders(db)
    template = get_or_create_email_template(db)
    snapshot: OutreachCampaign | None = None
    policy = outreach_policy_dict(settings)
    policy["sender_daily_limits"] = {
        str(sender.id): sender.daily_limit for sender in senders
    }
    if selection.candidates and senders:
        snapshot = OutreachCampaign(status="draft", filters=filters.model_dump(mode="json"), matched_count=selection.matched_count, recipient_count=len(selection.candidates), daily_limit=sum(sender.daily_limit for sender in senders), hourly_limit=0, min_interval_seconds=settings.outreach_message_interval_min_seconds, max_per_domain_per_day=0, subject_snapshot=template.subject_template, body_snapshot=template.body_template, recipients_snapshot=[{"company_id": item.company_id, "recipient": item.recipient} for item in selection.candidates], sender_account_ids=[sender.id for sender in senders], scheduler_settings=policy, snapshot_expires_at=now + timedelta(seconds=settings.outreach_snapshot_ttl_seconds))
        snapshot.deliveries.extend(OutreachDelivery(company_id=item.company_id, company_name=item.company_name, company_inn=item.company_inn, recipient=item.recipient, recipient_domain=item.recipient_domain, subject=item.subject, body=item.body) for item in selection.candidates)
        db.add(snapshot)
        db.commit()
        db.refresh(snapshot)
    sample = selection.candidates[0] if selection.candidates else None
    return {
        "matched_count": selection.matched_count,
        "eligible_count": selection.eligible_count,
        "selected_count": len(selection.candidates),
        "deferred_by_campaign_limit": max(0, selection.eligible_count - len(selection.candidates)),
        "skipped": selection.skipped,
        "sender_count": len(senders),
        "sender_emails": [sender.email for sender in senders],
        "mailru_configured": bool(senders),
        "snapshot_id": snapshot.id if snapshot else None,
        "snapshot_expires_at": _iso(snapshot.snapshot_expires_at) if snapshot else None,
        "policy": policy,
        "sample": {"company_name": sample.company_name, "recipient": sample.recipient, "subject": sample.subject, "body": sample.body} if sample else None,
    }


def active_outreach_campaign(db: Session) -> OutreachCampaign | None:
    return db.scalar(select(OutreachCampaign).where(OutreachCampaign.status.in_(ACTIVE_CAMPAIGN_STATUSES)).order_by(OutreachCampaign.id.desc()))


def confirm_outreach_campaign(db: Session, snapshot_id: int, settings: Settings, *, now: datetime | None = None) -> OutreachCampaign:
    timestamp = _aware(now) or datetime.now(timezone.utc)
    snapshot = db.get(OutreachCampaign, snapshot_id)
    if snapshot is None or snapshot.status != "draft":
        raise OutreachPolicyError("Снимок рассылки не найден или уже использован")
    expires_at = _aware(snapshot.snapshot_expires_at)
    if expires_at is None or expires_at < timestamp:
        snapshot.status = "stopped"
        snapshot.pause_reason = "Срок подтверждения снимка истёк"
        snapshot.completed_at = timestamp
        db.commit()
        raise OutreachPolicyError("Снимок устарел. Выполните предварительную проверку снова")
    if active_outreach_campaign(db):
        raise OutreachPolicyError("Другая рассылка уже выполняется или требует решения")
    available_ids = {sender.id for sender in _verified_senders(db)}
    if not snapshot.sender_account_ids or not all(sender_id in available_ids for sender_id in snapshot.sender_account_ids):
        raise OutreachPolicyError("Один из ящиков снимка больше не активен или не проверен")
    snapshot.status = "running"
    snapshot.confirmed_at = timestamp
    snapshot.started_at = timestamp
    snapshot.next_send_at = timestamp
    snapshot.current_round = 1
    snapshot.sender_position = 0
    snapshot.batch_position = 0
    snapshot.pause_reason = None
    db.commit()
    db.refresh(snapshot)
    logger.info("campaign_confirmed campaign_id=%s", snapshot.id)
    return snapshot


def create_outreach_campaign(db: Session, filters: CompanyFilters, settings: Settings) -> OutreachCampaign:
    preflight = build_outreach_preflight(db, filters, settings)
    if not preflight["snapshot_id"]:
        if not preflight["mailru_configured"]:
            raise OutreachPolicyError("Нет проверенных активных ящиков Mail.ru")
        raise OutreachPolicyError("Нет подходящих получателей")
    return confirm_outreach_campaign(db, preflight["snapshot_id"], settings)


def _delivery_counts(campaign: OutreachCampaign) -> Counter:
    return Counter(delivery.status for delivery in campaign.deliveries)


def outreach_campaign_to_dict(campaign: OutreachCampaign) -> dict:
    counts = _delivery_counts(campaign)
    processed = sum(counts[status] for status in TERMINAL_DELIVERY_STATUSES)
    remaining = counts["queued"] + counts["sending"]
    active_sender = next((delivery.sender_account_id for delivery in reversed(campaign.deliveries) if delivery.status == "sending"), campaign.current_batch_sender_id)
    return {
        "id": campaign.id,
        "status": campaign.status,
        "matched_count": campaign.matched_count,
        "recipient_count": campaign.recipient_count,
        "queued_count": counts["queued"], "sending_count": counts["sending"],
        "accepted_count": counts["accepted"], "sent_count": counts["accepted"],
        "failed_count": counts["failed"], "bounced_count": counts["bounced"],
        "uncertain_count": counts["uncertain"], "suppressed_count": counts["suppressed"],
        "cancelled_count": counts["cancelled"], "remaining_count": remaining,
        "progress_percent": round((processed / campaign.recipient_count) * 100 if campaign.recipient_count else 0, 1),
        "pause_reason": campaign.pause_reason,
        "current_round": campaign.current_round,
        "active_sender_account_id": active_sender,
        "active_sender_email": campaign.current_batch_sender.email if campaign.current_batch_sender else None,
        "sender_position": campaign.sender_position,
        "batch_position": campaign.batch_position,
        "current_batch_target": campaign.current_batch_target,
        "current_interval_seconds": campaign.current_interval_seconds,
        "next_send_at": _iso(campaign.next_send_at),
        "round_rest_until": _iso(campaign.round_rest_until),
        "last_sent_at": _iso(campaign.last_sent_at),
        "snapshot_expires_at": _iso(campaign.snapshot_expires_at),
        "started_at": _iso(campaign.started_at),
        "completed_at": _iso(campaign.completed_at),
        "created_at": _iso(campaign.created_at),
        "policy": campaign.scheduler_settings,
        "uncertain_deliveries": [
            {"id": item.id, "recipient": item.recipient}
            for item in campaign.deliveries
            if item.status == "uncertain"
        ],
        "acceptance_notice": "Принято SMTP-сервером не означает доставку или попадание во «Входящие».",
    }


def pause_outreach_campaign(db: Session, campaign: OutreachCampaign) -> OutreachCampaign:
    if campaign.status not in ("running", "cooldown"):
        raise OutreachPolicyError("На паузу можно поставить только активную рассылку")
    campaign.status = "paused"
    campaign.pause_reason = "Приостановлено пользователем"
    db.commit()
    return campaign


def resume_outreach_campaign(db: Session, campaign: OutreachCampaign) -> OutreachCampaign:
    if campaign.status != "paused":
        raise OutreachPolicyError("Продолжить можно только рассылку на паузе")
    if active := active_outreach_campaign(db):
        if active.id != campaign.id:
            raise OutreachPolicyError("Другая рассылка уже активна")
    now = datetime.now(timezone.utc)
    rest_until = _aware(campaign.round_rest_until)
    campaign.status = "cooldown" if rest_until and rest_until > now else "running"
    campaign.pause_reason = None
    if campaign.next_send_at is None:
        campaign.next_send_at = now
    db.commit()
    return campaign


def stop_outreach_campaign(db: Session, campaign: OutreachCampaign) -> OutreachCampaign:
    if campaign.status not in ACTIVE_CAMPAIGN_STATUSES:
        raise OutreachPolicyError("Эта рассылка уже необратимо завершена")
    queued = [delivery for delivery in campaign.deliveries if delivery.status == "queued"]
    for delivery in queued:
        delivery.status = "cancelled"
        delivery.error_message = "Не отправлено: кампания необратимо остановлена пользователем"
    campaign.cancelled_count += len(queued)
    campaign.status = "stopped"
    campaign.pause_reason = "Необратимо остановлено пользователем"
    campaign.completed_at = datetime.now(timezone.utc)
    campaign.next_send_at = None
    campaign.round_rest_until = None
    db.commit()
    return campaign


cancel_outreach_campaign = stop_outreach_campaign


def recover_interrupted_outreach(db: Session) -> None:
    interrupted = list(db.scalars(select(OutreachDelivery).where(OutreachDelivery.status == "sending")).all())
    for delivery in interrupted:
        delivery.status = "uncertain"
        delivery.error_message = "Результат SMTP-попытки неизвестен после перезапуска; автоматический повтор запрещён"
        delivery.claim_token = None
        campaign = delivery.campaign
        campaign.uncertain_count += 1
        campaign.status = "interrupted"
        campaign.pause_reason = "Worker был перезапущен во время SMTP-попытки. Требуется ручное решение"
        campaign.next_send_at = None
        campaign.worker_claim_token = None
        campaign.worker_claimed_at = None
    if interrupted:
        db.commit()


def assert_manual_send_allowed(db: Session, recipient: str, settings: Settings, *, now: datetime | None = None) -> None:
    del settings, now
    if active_outreach_campaign(db):
        raise OutreachPolicyError("Одиночная отправка недоступна, пока массовая рассылка активна")
    if normalize_email(recipient) in active_suppressed_addresses(db):
        raise OutreachPolicyError("Адрес находится в глобальных исключениях")


def _reset_sender_day(account: SenderAccount, now: datetime, settings: Settings) -> None:
    local_date = now.astimezone(settings.timezone).date()
    if account.sent_today_date != local_date:
        account.sent_today = 0
        account.sent_today_date = local_date


def _snapshot_sender_limit(campaign: OutreachCampaign, account: SenderAccount) -> int:
    limits = (campaign.scheduler_settings or {}).get("sender_daily_limits") or {}
    try:
        snapshotted = int(limits.get(str(account.id), account.daily_limit))
    except (TypeError, ValueError):
        snapshotted = account.daily_limit
    # A later safety reduction takes effect immediately; increasing the live
    # value never expands a campaign beyond its confirmed snapshot.
    return max(1, min(account.daily_limit, snapshotted))


def _snapshot_range(
    campaign: OutreachCampaign,
    key: str,
    fallback: tuple[int, int],
) -> tuple[int, int]:
    raw = (campaign.scheduler_settings or {}).get(key)
    if isinstance(raw, list) and len(raw) == 2:
        try:
            low, high = int(raw[0]), int(raw[1])
            if 0 < low <= high:
                return low, high
        except (TypeError, ValueError):
            pass
    return fallback


def _pre_send_suppression_reason(db: Session, delivery: OutreachDelivery) -> str | None:
    company = db.get(Company, delivery.company_id) if delivery.company_id else None
    if company is None:
        return "Компания удалена"
    if not company.is_active:
        return "Компания больше не действует"
    if company.status != "new":
        return "Статус компании больше не допускает отправку"
    current_addresses = {normalized for email in company.emails if (normalized := normalize_email(email.email))}
    if delivery.recipient not in current_addresses:
        return "Email компании изменён после снимка"
    if delivery.recipient in active_suppressed_addresses(db):
        return "Адрес находится в глобальных исключениях"
    if delivery.recipient in _previously_contacted_addresses(db):
        return "По этому адресу уже была SMTP-попытка без права автоповтора"
    return None


def _sync_campaign_counts(campaign: OutreachCampaign) -> Counter:
    counts = _delivery_counts(campaign)
    campaign.sent_count = counts["accepted"]
    campaign.accepted_count = counts["accepted"]
    campaign.failed_count = counts["failed"]
    campaign.bounced_count = counts["bounced"]
    campaign.uncertain_count = counts["uncertain"]
    campaign.suppressed_count = counts["suppressed"]
    campaign.cancelled_count = counts["cancelled"]
    return counts


def _complete_if_done(campaign: OutreachCampaign, now: datetime) -> bool:
    counts = _sync_campaign_counts(campaign)
    if counts["queued"] or counts["sending"]:
        return False
    campaign.next_send_at = None
    campaign.round_rest_until = None
    campaign.completed_at = now
    if campaign.status == "stopped":
        return True
    if counts["uncertain"]:
        campaign.status = "interrupted"
        campaign.pause_reason = "Есть отправка с неопределённым результатом"
    else:
        campaign.status = "completed"
        campaign.pause_reason = None
    return True


def _schedule_round_rest(campaign: OutreachCampaign, now: datetime, settings: Settings, random_int: Callable[[int, int], int]) -> float:
    minimum, maximum = _snapshot_range(
        campaign,
        "round_rest_minutes",
        (settings.outreach_round_rest_min_minutes, settings.outreach_round_rest_max_minutes),
    )
    minutes = random_int(minimum, maximum)
    campaign.status = "cooldown"
    campaign.sender_position = 0
    campaign.batch_position = 0
    campaign.current_batch_target = 0
    campaign.current_batch_sender_id = None
    campaign.current_interval_seconds = None
    campaign.round_rest_until = now + timedelta(minutes=minutes)
    campaign.next_send_at = campaign.round_rest_until
    return float(minutes * 60)


def _sender_for_current_position(db: Session, campaign: OutreachCampaign, settings: Settings, now: datetime) -> SenderAccount | None:
    sender_ids = campaign.sender_account_ids or []
    while campaign.sender_position < len(sender_ids):
        account = db.get(SenderAccount, sender_ids[campaign.sender_position])
        if account:
            _reset_sender_day(account, now, settings)
            if account.verification_status == "temporary_error" and account.blocked_until_round is not None and campaign.current_round > account.blocked_until_round:
                account.verification_status = "verified"
                account.verification_error = None
                account.block_reason = None
        daily_limit = _snapshot_sender_limit(campaign, account) if account else 0
        eligible = bool(account and account.is_active and account.smtp_enabled and account.verification_status == "verified" and account.encrypted_password and account.sent_today < daily_limit and not (account.blocked_until_round is not None and campaign.current_round <= account.blocked_until_round))
        if eligible:
            account.current_batch_size = batch_size_for_successes(account.successful_full_batches)
            return account
        campaign.sender_position += 1
        campaign.batch_position = 0
        campaign.current_batch_target = 0
        campaign.current_batch_sender_id = None
    return None


def _advance_sender(campaign: OutreachCampaign) -> None:
    campaign.sender_position += 1
    campaign.batch_position = 0
    campaign.current_batch_target = 0
    campaign.current_batch_sender_id = None
    campaign.current_interval_seconds = None


def _record_bounce_suppression(db: Session, delivery: OutreachDelivery, smtp_code: str | None, now: datetime) -> None:
    suppression = db.scalar(select(EmailSuppression).where(EmailSuppression.email == delivery.recipient))
    if suppression is None:
        suppression = EmailSuppression(email=delivery.recipient, reason="Подтверждён постоянный отказ адреса", source="smtp")
        db.add(suppression)
    suppression.reason = "Подтверждён постоянный отказ адреса"
    suppression.source = "smtp"
    suppression.campaign_id = delivery.campaign_id
    suppression.delivery_id = delivery.id
    suppression.smtp_code = smtp_code
    suppression.created_at = now
    suppression.lifted_at = None


def _claim_next_delivery(db: Session, campaign: OutreachCampaign, settings: Settings, now: datetime, random_int: Callable[[int, int], int]) -> tuple[OutreachDelivery, SenderAccount, str] | None:
    if campaign.worker_claim_token:
        return None
    while True:
        if _complete_if_done(campaign, now):
            db.commit()
            return None
        account = _sender_for_current_position(db, campaign, settings, now)
        if account is None:
            _schedule_round_rest(campaign, now, settings, random_int)
            db.commit()
            return None
        queued_count = db.scalar(select(func.count(OutreachDelivery.id)).where(OutreachDelivery.campaign_id == campaign.id, OutreachDelivery.status == "queued")) or 0
        remaining_daily = _snapshot_sender_limit(campaign, account) - account.sent_today
        if campaign.current_batch_sender_id != account.id or campaign.current_batch_target <= 0:
            campaign.current_batch_sender_id = account.id
            campaign.current_batch_target = min(account.current_batch_size, remaining_daily, queued_count)
            campaign.batch_position = 0
        if campaign.batch_position >= campaign.current_batch_target:
            if campaign.current_batch_target == account.current_batch_size:
                account.successful_full_batches += 1
                account.current_batch_size = batch_size_for_successes(account.successful_full_batches)
            _advance_sender(campaign)
            continue
        delivery = db.scalar(select(OutreachDelivery).where(OutreachDelivery.campaign_id == campaign.id, OutreachDelivery.status == "queued").order_by(OutreachDelivery.id.asc()).with_for_update(skip_locked=True))
        if delivery is None:
            _complete_if_done(campaign, now)
            db.commit()
            return None
        if reason := _pre_send_suppression_reason(db, delivery):
            delivery.status = "suppressed"
            delivery.error_message = reason
            _sync_campaign_counts(campaign)
            db.commit()
            continue
        token = uuid4().hex
        delivery.status = "sending"
        delivery.sender_account_id = account.id
        delivery.started_at = now
        delivery.claimed_at = now
        delivery.claim_token = token
        delivery.error_message = None
        campaign.worker_claim_token = token
        campaign.worker_claimed_at = now
        campaign.next_send_at = None
        db.commit()
        return delivery, account, token


def _apply_accepted(db: Session, delivery: OutreachDelivery, campaign: OutreachCampaign, account: SenderAccount, result: SMTPAccepted, now: datetime, settings: Settings, random_int: Callable[[int, int], int]) -> float:
    delivery.status = "accepted"
    delivery.message_id = result.message_id
    delivery.smtp_code = result.smtp_code
    delivery.smtp_response = result.smtp_response
    delivery.sent_at = now
    delivery.accepted_at = now
    delivery.claim_token = None
    account.sent_today += 1
    account.last_sent_at = now
    account.block_reason = None
    campaign.last_sent_at = now
    campaign.batch_position += 1
    company = db.get(Company, delivery.company_id) if delivery.company_id else None
    if company is not None:
        previous_status = company.status
        company.status = "sent"
        company.last_updated_at = now
        db.add(ActivityHistory(company=company, event_type="email_sent", description=f"SMTP-сервер принял письмо на {delivery.recipient}", from_status=previous_status, to_status="sent", event_data={"recipient": delivery.recipient, "message_id": result.message_id, "campaign_id": campaign.id, "sender_account_id": account.id, "smtp_code": result.smtp_code, "sent_copy_saved": result.sent_copy_saved}, created_at=now))
    batch_completed = campaign.batch_position >= campaign.current_batch_target
    if batch_completed and campaign.current_batch_target == account.current_batch_size:
        account.successful_full_batches += 1
        account.current_batch_size = batch_size_for_successes(account.successful_full_batches)
    if _complete_if_done(campaign, now) or campaign.status == "stopped":
        return float(settings.outreach_worker_poll_seconds)
    if batch_completed:
        _advance_sender(campaign)
        campaign.next_send_at = now
        return 1.0
    minimum, maximum = _snapshot_range(
        campaign,
        "message_interval_seconds",
        (settings.outreach_message_interval_min_seconds, settings.outreach_message_interval_max_seconds),
    )
    seconds = random_int(minimum, maximum)
    delivery.interval_seconds = seconds
    campaign.current_interval_seconds = seconds
    campaign.next_send_at = now + timedelta(seconds=seconds)
    return float(seconds)


def _apply_smtp_error(db: Session, delivery: OutreachDelivery, campaign: OutreachCampaign, account: SenderAccount, error: SMTPDeliveryError, now: datetime, settings: Settings, random_int: Callable[[int, int], int]) -> float:
    was_stopped = campaign.status == "stopped"
    delivery.claim_token = None
    delivery.smtp_code = error.smtp_code
    delivery.error_message = error.safe_message
    if error.uncertain:
        delivery.status = "uncertain"
        if not was_stopped:
            campaign.status = "interrupted"
            campaign.pause_reason = "Есть SMTP-попытка с неопределённым результатом. Автоповтор запрещён"
        campaign.next_send_at = None
        account.blocked_until_round = campaign.current_round + 3
        account.block_reason = "Неопределённый результат SMTP-попытки"
    elif error.permanent_recipient_failure:
        delivery.status = "bounced"
        _record_bounce_suppression(db, delivery, error.smtp_code, now)
        mark_company_send_failed(db, delivery.company_id, delivery.recipient, error.safe_message, campaign_id=campaign.id, occurred_at=now)
        _advance_sender(campaign)
        campaign.next_send_at = now
    else:
        delivery.status = "failed"
        account.blocked_until_round = campaign.current_round + 3
        account.block_reason = error.safe_message
        account.verification_status = "blocked" if error.category == "auth" else "temporary_error"
        account.verification_error = error.safe_message
        mark_company_send_failed(db, delivery.company_id, delivery.recipient, error.safe_message, campaign_id=campaign.id, occurred_at=now)
        _advance_sender(campaign)
        campaign.next_send_at = now
    _sync_campaign_counts(campaign)
    if was_stopped:
        campaign.status = "stopped"
        campaign.next_send_at = None
        campaign.round_rest_until = None
        return float(settings.outreach_worker_poll_seconds)
    if error.uncertain or _complete_if_done(campaign, now):
        return float(settings.outreach_worker_poll_seconds)
    if campaign.sender_position >= len(campaign.sender_account_ids or []):
        return _schedule_round_rest(campaign, now, settings, random_int)
    return 1.0


def process_outreach_tick(settings: Settings, *, sender_factory: Callable = MailruSMTPClient, session_factory: Callable[[], Session] | None = None, now: datetime | None = None, random_int: Callable[[int, int], int] = random.randint) -> float:
    effective_now = _aware(now) or datetime.now(timezone.utc)
    factory = session_factory or SessionLocal
    with factory() as db:
        campaign = db.scalar(select(OutreachCampaign).where(OutreachCampaign.status.in_(("running", "cooldown"))).order_by(OutreachCampaign.id.asc()).with_for_update(skip_locked=True))
        if campaign is None or campaign.worker_claim_token:
            return float(settings.outreach_worker_poll_seconds)
        scheduled_at = _aware(campaign.next_send_at)
        if scheduled_at and scheduled_at > effective_now:
            return max(1.0, (scheduled_at - effective_now).total_seconds())
        if campaign.status == "cooldown":
            campaign.status = "running"
            campaign.current_round += 1
            campaign.round_rest_until = None
            campaign.next_send_at = effective_now
        claim = _claim_next_delivery(db, campaign, settings, effective_now, random_int)
        if claim is None:
            return float(settings.outreach_worker_poll_seconds)
        delivery, account, token = claim
        delivery_id, campaign_id, account_id = delivery.id, campaign.id, account.id
        recipient, subject, body = delivery.recipient, delivery.subject, delivery.body
    try:
        with factory() as credential_db:
            stored_account = credential_db.get(SenderAccount, account_id)
            if stored_account is None:
                raise CredentialEncryptionError("Ящик удалён до отправки")
            password = CredentialCipher(settings.mail_credentials_encryption_key).decrypt(stored_account.encrypted_password)
            client = sender_factory(
                stored_account,
                password,
                timeout_seconds=settings.mail_smtp_timeout_seconds,
                imap_timeout_seconds=settings.mail_imap_timeout_seconds,
            )
            result = client.send(recipient, subject, body, delivery_id=delivery_id, campaign_id=campaign_id)
            if isinstance(result, str):
                result = SMTPAccepted(result, "250", "accepted")
    except SMTPDeliveryError as exc:
        error, result = exc, None
    except (CredentialEncryptionError, ValueError):
        error, result = SMTPDeliveryError("Не удалось безопасно использовать пароль ящика", category="credentials"), None
    except Exception:
        error, result = SMTPDeliveryError("Неизвестная ошибка SMTP-транспорта", category="provider"), None
    finished_at = effective_now if now is not None else datetime.now(timezone.utc)
    with factory() as db:
        delivery = db.scalar(select(OutreachDelivery).where(OutreachDelivery.id == delivery_id, OutreachDelivery.claim_token == token).with_for_update())
        campaign = db.get(OutreachCampaign, campaign_id)
        account = db.get(SenderAccount, account_id)
        if delivery is None or campaign is None or account is None:
            return float(settings.outreach_worker_poll_seconds)
        campaign.worker_claim_token = None
        campaign.worker_claimed_at = None
        if result is not None:
            delay = _apply_accepted(db, delivery, campaign, account, result, finished_at, settings, random_int)
            outcome = "accepted"
        else:
            delay = _apply_smtp_error(db, delivery, campaign, account, error, finished_at, settings, random_int)
            outcome = delivery.status
        db.commit()
        logger.info("smtp_attempt_finished campaign_id=%s delivery_id=%s sender_account_id=%s outcome=%s", campaign_id, delivery_id, account_id, outcome)
        return delay


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
                logger.exception("outreach_worker_tick_failed")
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
