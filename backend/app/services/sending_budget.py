"""Rolling campaign budgets from durable sends, shared across campaigns.

Count known attempts even when rejected; midnight and restarts do not reset
provider traffic. Recorded manual sends and replies consume the same budget.
These are local safeguards, never a provider delivery guarantee.
"""
from datetime import timedelta, timezone

from sqlalchemy import func, or_, select

from app.models import ActivityHistory, EmailReplyAttempt, OutreachDelivery


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def sender_budget_retry_at(db, account, now, *, hourly_limit, daily_limit):
    now = _aware(now)
    cutoff = now - timedelta(days=1)
    stamps = {}
    at = func.coalesce(OutreachDelivery.sent_at, OutreachDelivery.started_at)
    for row_id, message_id, timestamp in db.execute(select(
        OutreachDelivery.id, OutreachDelivery.message_id, at,
    ).where(OutreachDelivery.sender_account_id == account.id, at > cutoff)):
        stamps[message_id or f"delivery:{row_id}"] = _aware(timestamp)
    for row_id, message_id, timestamp in db.execute(select(
        EmailReplyAttempt.id, EmailReplyAttempt.message_id, EmailReplyAttempt.created_at,
    ).where(EmailReplyAttempt.sender_account_id == account.id, EmailReplyAttempt.created_at > cutoff)):
        stamps[message_id or f"reply:{row_id}"] = _aware(timestamp)
    for event in db.scalars(select(ActivityHistory).where(
        ActivityHistory.event_type == "email_sent", ActivityHistory.created_at > cutoff,
        or_(ActivityHistory.event_data["sender_account_id"].as_integer() == account.id,
            ActivityHistory.event_data["sender_email"].as_string() == account.email),
    )):
        key = (event.event_data or {}).get("message_id") or f"history:{event.id}"
        # A transport receipt has the authoritative attempt timestamp.
        stamps.setdefault(key, _aware(event.created_at))
    waits = []
    for window, limit in ((timedelta(hours=1), hourly_limit), (timedelta(days=1), daily_limit)):
        recent = sorted(t for t in stamps.values() if t > now - window)
        if len(recent) >= limit:
            waits.append(recent[-limit] + window)
    return max(waits) if waits else None
