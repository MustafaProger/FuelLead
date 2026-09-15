"""An explicit inbox refresh coalesces concurrent browser requests."""
from datetime import datetime, timezone
from threading import Lock

from sqlalchemy import select

from app.models import SenderAccount
from app.services.imap_bounces import process_imap_tick, process_reply_folders_tick

_lock = Lock()
_finished_at = None
_error = None


def sync_state(db, settings):
    accounts = list(db.scalars(select(SenderAccount).where(SenderAccount.is_active.is_(True), SenderAccount.imap_enabled.is_(True))))
    return {"running": _lock.locked(), "finished_at": _finished_at, "error": _error,
            "poll_seconds": settings.mail_imap_worker_poll_seconds,
            "mailboxes": [{"email": a.email, "status": a.imap_verification_status,
                           "error": a.imap_verification_error} for a in accounts]}


def start_sync(background_tasks, settings):
    if not _lock.acquire(blocking=False):
        return {"started": False}
    background_tasks.add_task(_sync, settings)
    return {"started": True}


def _sync(settings):
    global _finished_at, _error
    _error = None
    try:
        process_imap_tick(settings)
        process_reply_folders_tick(settings)
        _finished_at = datetime.now(timezone.utc).isoformat()
    except Exception:
        _error = "Не удалось обновить почту. Проверьте состояние ящиков"
    finally:
        _lock.release()
