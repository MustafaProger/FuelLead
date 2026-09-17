"""Read company correspondence and send durable, explicitly requested replies."""
import re
from datetime import datetime, timezone
from email.utils import make_msgid

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ActivityHistory, Company, EmailReplyAttempt, EmailSuppression, OutreachDelivery, SenderAccount
from app.schemas import ConversationReplyRequest
from app.serializers import as_aware
from app.services.credentials import CredentialCipher, CredentialEncryptionError
from app.services.mail_dispatch import mail_dispatch_slot
from app.services.outreach import defer_outreach_after_reply, wake_outreach_worker
from app.services.provider import normalize_email
from app.services.smtp import MailruSMTPClient, SMTPDeliveryError


def _company(db, company_id):
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Компания не найдена")
    return company


def _unread():
    return (ActivityHistory.event_type == "email_reply") & ActivityHistory.event_data["read_at"].as_string().is_(None)


def conversation_list(db: Session, *, search="", unread=False, page=1, page_size=30):
    latest = select(ActivityHistory.company_id, func.max(ActivityHistory.id).label("latest_id")).where(
        ActivityHistory.event_type == "email_reply").group_by(ActivityHistory.company_id).subquery()
    unread_counts = select(ActivityHistory.company_id, func.count().label("count")).where(_unread()).group_by(ActivityHistory.company_id).subquery()
    query = select(Company, ActivityHistory, func.coalesce(unread_counts.c.count, 0)).join(
        latest, latest.c.company_id == Company.id).join(ActivityHistory, ActivityHistory.id == latest.c.latest_id).outerjoin(
        unread_counts, unread_counts.c.company_id == Company.id)
    if search.strip():
        pattern = "%" + search.strip().replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(or_(Company.name.ilike(pattern, escape="\\"), Company.inn.ilike(pattern, escape="\\"),
                               ActivityHistory.event_data["sender"].as_string().ilike(pattern, escape="\\")))
    if unread:
        query = query.where(unread_counts.c.count > 0)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.execute(query.order_by(ActivityHistory.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [{"company_id": c.id, "company_name": c.name, "status": c.status,
                       "sender": (h.event_data or {}).get("sender", ""),
                       "subject": (h.event_data or {}).get("subject") or "Без темы",
                       "preview": (h.event_data or {}).get("text", "")[:220],
                       "is_automatic": bool((h.event_data or {}).get("is_automatic")),
                       "received_at": as_aware(h.created_at).isoformat(), "unread_count": n}
                      for c, h, n in rows], "total": total, "page": page,
            "unread_count": db.scalar(select(func.count()).select_from(ActivityHistory).where(_unread())) or 0}


def _reply_target(db, company, event):
    data = event.event_data or {}
    account = db.get(SenderAccount, data.get("sender_account_id")) if data.get("sender_account_id") else None
    recipient = normalize_email(data.get("reply_to") or data.get("sender") or "")
    reason = None
    if company.status == "rejected":
        reason = "Компания отказалась от дальнейших писем"
    elif not recipient:
        reason = "В письме не указан корректный адрес для ответа"
    elif db.scalar(select(EmailSuppression.id).where(EmailSuppression.email == recipient, EmailSuppression.lifted_at.is_(None))):
        reason = "Адрес находится в исключениях"
    elif not account or not account.is_active or not account.smtp_enabled or account.verification_status != "verified":
        reason = "Подключите и проверьте исходный почтовый ящик в разделе «Почтовые ящики»"
    return account, recipient, reason


def conversation_detail(db, company_id):
    company = _company(db, company_id)
    events = list(db.scalars(select(ActivityHistory).where(ActivityHistory.company_id == company_id,
        ActivityHistory.event_type.in_(("email_reply", "email_sent"))).order_by(ActivityHistory.id)))
    deliveries = {d.message_id: d for d in db.scalars(select(OutreachDelivery).where(
        OutreachDelivery.company_id == company_id, OutreachDelivery.message_id.is_not(None)))}
    accounts = {a.id: a.email for a in db.scalars(select(SenderAccount))}
    messages = []
    latest_reply_id = None
    for event in events:
        data = event.event_data or {}
        inbound = event.event_type == "email_reply"
        delivery = deliveries.get(data.get("message_id")) if not inbound else None
        account, recipient, reason = _reply_target(db, company, event) if inbound else (None, None, None)
        if inbound:
            latest_reply_id = event.id
        messages.append({"id": str(event.id), "reply_id": event.id if inbound else None,
            "direction": "incoming" if inbound else "outgoing", "status": "received" if inbound else "accepted",
            "sender": data.get("sender", "") if inbound else data.get("sender_email") or accounts.get(data.get("sender_account_id"), ""),
            "recipient": accounts.get(data.get("sender_account_id"), "") if inbound else data.get("recipient", ""),
            "subject": data.get("subject") or (delivery.subject if delivery else "Без темы"),
            "body": data.get("body") or (delivery.body if delivery else data.get("text")) or "Текст этого старого письма не сохранён",
            "preview": data.get("text", ""), "legacy": inbound and not data.get("content_version"),
            "is_automatic": inbound and bool(data.get("is_automatic")),
            "attachments": data.get("attachments", []), "created_at": data.get("sent_at") or as_aware(event.created_at).isoformat(),
            "unread": inbound and not data.get("read_at"), "reply_recipient": recipient,
            "reply_sender": account.email if account else None, "reply_disabled_reason": reason,
            "sent_copy_saved": data.get("sent_copy_saved"), "error": None})
    for attempt in db.scalars(select(EmailReplyAttempt).where(EmailReplyAttempt.company_id == company_id,
                                                           EmailReplyAttempt.status != "accepted")):
        messages.append({**attempt_to_dict(attempt), "id": attempt.id, "direction": "outgoing", "sender": attempt.sender_email,
                         "created_at": as_aware(attempt.created_at).isoformat(), "unread": False, "attachments": []})
    order = {m["id"]: (as_aware(datetime.fromisoformat(m["created_at"])).timestamp(), 0, m["id"])
             for m in messages}
    outgoing_times = {((e.event_data or {}).get("sender_account_id"), (e.event_data or {}).get("message_id")):
                      order[str(e.id)][0] for e in events if e.event_type == "email_sent"}
    for event in events:
        if event.event_type != "email_reply":
            continue
        data = event.event_data or {}
        parent_times = [outgoing_times[(data.get("sender_account_id"), reference)]
                        for reference in (data.get("references") or [])
                        if (data.get("sender_account_id"), reference) in outgoing_times]
        key = str(event.id)
        if parent_times and max(parent_times) >= order[key][0]:
            # A fast auto-reply can be dated before SMTP completion is recorded.
            # Keep its real Date visible, but show it after the referenced offer.
            order[key] = (max(parent_times), 1, key)
    messages.sort(key=lambda m: order[m["id"]])
    return {"company_id": company.id, "company_name": company.name, "company_status": company.status,
            "messages": messages, "latest_reply_id": latest_reply_id}


def mark_read(db, company_id, through_id):
    _company(db, company_id)
    now = datetime.now(timezone.utc).isoformat()
    for event in db.scalars(select(ActivityHistory).where(ActivityHistory.company_id == company_id,
        ActivityHistory.id <= through_id, _unread()).with_for_update()):
        event.event_data = {**(event.event_data or {}), "read_at": now}
    db.commit()
    return {"ok": True}


def attempt_to_dict(attempt):
    status = attempt.status
    if status == "sending" and (datetime.now(timezone.utc) - as_aware(attempt.created_at)).total_seconds() > 300:
        status = "uncertain"
    return {"request_id": attempt.id, "status": status, "message_id": attempt.message_id,
            "recipient": attempt.recipient, "subject": attempt.subject, "body": attempt.body,
            "error": attempt.error, "sent_copy_saved": attempt.sent_copy_saved}


def send_reply(db: Session, company_id: int, request: ConversationReplyRequest, settings, *, sender_factory=MailruSMTPClient):
    try:
        with mail_dispatch_slot(db, reply=True):
            return _send_reply(db, company_id, request, settings, sender_factory=sender_factory)
    finally:
        wake_outreach_worker()


def _send_reply(db: Session, company_id: int, request: ConversationReplyRequest, settings, *, sender_factory):
    company = _company(db, company_id)
    request_id = str(request.request_id)
    existing = db.get(EmailReplyAttempt, request_id)
    if existing:
        if existing.company_id != company_id or existing.reply_id != request.reply_id or existing.body != request.body:
            raise HTTPException(409, "Этот запрос уже использован для другого письма")
        return attempt_to_dict(existing)
    event = db.get(ActivityHistory, request.reply_id)
    if not event or event.company_id != company_id or event.event_type != "email_reply":
        raise HTTPException(404, "Входящее письмо не найдено")
    account, recipient, reason = _reply_target(db, company, event)
    if reason:
        raise HTTPException(409, reason)
    # Serialize reservations per mailbox; release before the network operation.
    db.refresh(company, with_for_update=True)
    db.refresh(account, with_for_update=True)
    existing = db.get(EmailReplyAttempt, request_id, populate_existing=True)
    if existing:
        if existing.company_id != company_id or existing.reply_id != request.reply_id or existing.body != request.body:
            raise HTTPException(409, "Этот запрос уже использован для другого письма")
        return attempt_to_dict(existing)
    account, recipient, reason = _reply_target(db, company, event)
    if reason:
        raise HTTPException(409, reason)
    if db.scalar(select(EmailReplyAttempt.id).where(EmailReplyAttempt.sender_account_id == account.id,
                                                   EmailReplyAttempt.status.in_(("sending", "uncertain")))):
        raise HTTPException(409, "В этом ящике есть незавершённая отправка. Проверьте её результат перед новым письмом")
    if db.scalar(select(OutreachDelivery.id).where(OutreachDelivery.status == "sending")):
        raise HTTPException(409, "Предыдущая отправка рассылки ещё не завершена. Проверьте её результат")
    local_date = datetime.now(settings.timezone).date()
    if account.sent_today_date != local_date:
        account.sent_today_date, account.sent_today = local_date, 0
    if account.sent_today >= account.daily_limit:
        raise HTTPException(429, "Достигнут дневной лимит этого ящика")
    data = event.event_data or {}
    subject = " ".join(str(data.get("subject") or "Переписка").split())[:990]
    if not re.match(r"(?i)^re\s*:", subject):
        subject = "Re: " + subject
    valid_id = lambda value: isinstance(value, str) and re.fullmatch(r"<[^<>\s\x00-\x1f\x7f]{1,250}>", value)
    parent_id = data.get("message_id") if valid_id(data.get("message_id")) else None
    references = tuple(v for v in [*(data.get("references") or []), parent_id] if valid_id(v))[-20:]
    try:
        password = CredentialCipher(settings.mail_credentials_encryption_key).decrypt(account.encrypted_password)
    except CredentialEncryptionError as exc:
        raise HTTPException(503, "Сохраните пароль приложения для этого ящика заново") from exc
    attempt = EmailReplyAttempt(id=request_id, company_id=company_id, reply_id=event.id,
        sender_account_id=account.id, sender_email=account.email, recipient=recipient, subject=subject,
        body=request.body, message_id=make_msgid(domain=account.email.rsplit("@", 1)[1]), status="sending")
    db.add(attempt)
    account.sent_today += 1  # reserve quota, including uncertain transmissions
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.get(EmailReplyAttempt, request_id)
        if existing and existing.company_id == company_id and existing.reply_id == request.reply_id and existing.body == request.body:
            return attempt_to_dict(existing)
        raise HTTPException(409, "Письмо уже отправляется")
    try:
        result = sender_factory(account, password, timeout_seconds=settings.mail_smtp_timeout_seconds,
            imap_timeout_seconds=settings.mail_imap_timeout_seconds).send(recipient, subject, request.body,
            in_reply_to=parent_id, references=references, message_id=attempt.message_id)
    except Exception as exc:
        # Unknown errors after reservation cannot establish that DATA was not accepted.
        uncertain = not isinstance(exc, SMTPDeliveryError) or exc.uncertain
        attempt.status = "uncertain" if uncertain else "failed"
        attempt.error = exc.safe_message if isinstance(exc, SMTPDeliveryError) else "Результат отправки неизвестен. Проверьте «Отправленные» перед повтором"
        if not uncertain:
            db.refresh(account, with_for_update=True)
            if account.sent_today_date == local_date:
                account.sent_today = max(0, account.sent_today - 1)
        db.commit()
        return attempt_to_dict(attempt)
    attempt.status, attempt.sent_copy_saved = "accepted", result.sent_copy_saved
    sent_at = datetime.now(timezone.utc)
    db.refresh(company, with_for_update=True)
    db.refresh(account, with_for_update=True)
    account.last_sent_at = sent_at
    defer_outreach_after_reply(db, account.id, sent_at, settings)
    db.add(ActivityHistory(company_id=company_id, event_type="email_sent", description=f"Ответ отправлен на {recipient}",
        from_status=company.status, to_status=company.status,
        event_data={"recipient": recipient, "sender_account_id": account.id, "sender_email": account.email,
                    "subject": subject, "body": request.body, "message_id": attempt.message_id,
                    "in_reply_to": parent_id, "sent_copy_saved": result.sent_copy_saved, "request_id": request_id}))
    company.last_updated_at = datetime.now(timezone.utc)
    db.commit()
    return attempt_to_dict(attempt)


def resolve_reply(db, company_id, request_id, outcome):
    try:
        with mail_dispatch_slot(db, reply=True):
            return _resolve_reply(db, company_id, request_id, outcome)
    finally:
        wake_outreach_worker()


def _resolve_reply(db, company_id, request_id, outcome):
    attempt = db.scalar(select(EmailReplyAttempt).where(EmailReplyAttempt.id == request_id,
        EmailReplyAttempt.company_id == company_id).with_for_update())
    if not attempt:
        raise HTTPException(404, "Отправка не найдена")
    if attempt_to_dict(attempt)["status"] != "uncertain":
        raise HTTPException(409, "Можно уточнить только неизвестный результат отправки")
    attempt.status = outcome
    attempt.error = None if outcome == "accepted" else "Оператор подтвердил, что письмо не отправлено"
    if outcome == "accepted":
        if not db.scalar(select(ActivityHistory.id).where(ActivityHistory.company_id == company_id,
                          ActivityHistory.event_data["request_id"].as_string() == request_id)):
            db.add(ActivityHistory(company_id=company_id, event_type="email_sent",
                description=f"Отправка ответа на {attempt.recipient} подтверждена оператором",
                created_at=attempt.created_at,
                event_data={"request_id": request_id, "message_id": attempt.message_id,
                            "sender_account_id": attempt.sender_account_id, "sender_email": attempt.sender_email,
                            "recipient": attempt.recipient, "subject": attempt.subject, "body": attempt.body}))
    db.commit()
    return attempt_to_dict(attempt)
