"""One SMTP operation at a time, with waiting client replies ahead of outreach.

PostgreSQL session locks survive the commits made before/after SMTP. They use a
dedicated connection which never holds an open transaction during network I/O.
The in-process fallback is for SQLite's single-process local/test runtime.
"""
from contextlib import contextmanager
from threading import Condition


_condition = Condition()
_busy = False
_waiting_replies = 0


@contextmanager
def mail_dispatch_slot(db, *, reply: bool):
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        with bind.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            priority = acquired = False
            try:
                if reply:
                    connection.exec_driver_sql("SELECT pg_advisory_lock_shared(701337, 20260916)")
                    priority = True
                    connection.exec_driver_sql("SELECT pg_advisory_lock(701337, 20260915)")
                    acquired = True
                else:
                    priority = bool(connection.exec_driver_sql(
                        "SELECT pg_try_advisory_lock(701337, 20260916)").scalar())
                    if priority:
                        acquired = bool(connection.exec_driver_sql(
                            "SELECT pg_try_advisory_lock(701337, 20260915)").scalar())
                        # Let replies register their priority while this SMTP
                        # operation is in flight, preventing the next tick.
                        connection.exec_driver_sql("SELECT pg_advisory_unlock(701337, 20260916)")
                        priority = False
                yield acquired
            finally:
                try:
                    if acquired:
                        connection.exec_driver_sql("SELECT pg_advisory_unlock(701337, 20260915)")
                    if priority:
                        connection.exec_driver_sql(
                            "SELECT pg_advisory_unlock_shared(701337, 20260916)" if reply
                            else "SELECT pg_advisory_unlock(701337, 20260916)")
                except Exception:
                    # A pooled connection must never retain a session lock.
                    connection.invalidate()
                    raise
        return

    global _busy, _waiting_replies
    with _condition:
        if reply:
            _waiting_replies += 1
            try:
                _condition.wait_for(lambda: not _busy)
                acquired = True
            finally:
                _waiting_replies -= 1
        else:
            acquired = not _busy and not _waiting_replies
        if acquired:
            _busy = True
    try:
        yield acquired
    finally:
        if acquired:
            with _condition:
                _busy = False
                _condition.notify_all()
