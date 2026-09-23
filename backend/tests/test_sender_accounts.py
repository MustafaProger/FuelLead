from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.models import OutreachCampaign, OutreachDelivery, SenderAccount
from app.schemas import SenderAccountCreate, SenderAccountUpdate, SenderTestEmailRequest
from app.services.credentials import CredentialCipher, generate_encryption_key
from app.services.sender_accounts import (
    SenderAccountError,
    batch_size_for_successes,
    create_sender_account,
    send_test_message,
    send_test_message_and_reconcile,
    sender_account_to_dict,
    sender_used_by_active_campaign,
    update_sender_account,
    verify_sender_account,
)
from app.services.imap_bounces import IMAPCollectorError
from app.services.smtp import SMTPAccepted, SMTPDeliveryError


def settings():
    return Settings(_env_file=None, mail_credentials_encryption_key=generate_encryption_key())


def test_password_is_encrypted_and_never_serialized(db):
    app_settings = settings()
    account = create_sender_account(
        db,
        SenderAccountCreate(email="owner@mail.ru", display_name="Owner", password="new-app-password", daily_limit=40),
        app_settings,
    )
    payload = sender_account_to_dict(account)

    assert account.encrypted_password != "new-app-password"
    assert CredentialCipher(app_settings.mail_credentials_encryption_key).decrypt(account.encrypted_password) == "new-app-password"
    assert payload["password_saved"] is True
    assert "password" not in payload
    assert "encrypted_password" not in payload
    assert "new-app-password" not in str(payload)


@pytest.mark.parametrize("app_timezone,count_date,now,expected", [
    ("Europe/Moscow", "2026-09-22", "2026-09-22T20:59:59+00:00", 50),
    ("Europe/Moscow", "2026-09-22", "2026-09-22T21:00:00+00:00", 0),
    ("Europe/Moscow", "2026-09-23", "2026-09-22T21:00:00+00:00", 50),
    ("Europe/Moscow", "2026-09-22", "2026-09-23T09:00:00+00:00", 0),
    ("America/Los_Angeles", "2026-09-22", "2026-09-23T06:59:59+00:00", 50),
    ("America/Los_Angeles", "2026-09-22", "2026-09-23T07:00:00+00:00", 0),
    ("Europe/Moscow", None, "2026-09-23T09:00:00+00:00", 50),
    ("Europe/Moscow", "2026-09-24", "2026-09-23T09:00:00+00:00", 50),
    ("Europe/Moscow", "2026-09-22", "2026-09-22T21:00:00", 0),
])
def test_serialized_today_count_uses_local_date_without_mutating_account(db, app_timezone, count_date, now, expected):
    app_settings = settings()
    app_settings.app_timezone = app_timezone
    account = create_sender_account(db, SenderAccountCreate(email="owner@mail.ru", password="secret"), app_settings)
    account.sent_today = 50
    account.sent_today_date = date.fromisoformat(count_date) if count_date else None
    db.commit()
    db.refresh(account)
    accounting = (account.sent_today, account.sent_today_date, account.updated_at)

    payload = sender_account_to_dict(account, settings=app_settings, now=datetime.fromisoformat(now))

    assert payload["sent_today"] == expected
    assert (account.sent_today, account.sent_today_date, account.updated_at) == accounting
    assert not db.is_modified(account)
    db.refresh(account)
    assert (account.sent_today, account.sent_today_date, account.updated_at) == accounting


def test_serialized_today_count_defaults_to_configured_app_timezone(db, monkeypatch):
    app_settings = settings()
    app_settings.app_timezone = "America/Los_Angeles"
    account = create_sender_account(db, SenderAccountCreate(email="owner@mail.ru", password="secret"), app_settings)
    account.sent_today = 17
    account.sent_today_date = date(2026, 9, 22)
    db.commit()
    monkeypatch.setattr("app.services.sender_accounts.get_settings", lambda: app_settings)

    payload = sender_account_to_dict(account, now=datetime(2026, 9, 23, 1, tzinfo=timezone.utc))

    assert payload["sent_today"] == 17


def test_replacing_password_invalidates_verification_and_does_not_return_it(db):
    app_settings = settings()
    account = create_sender_account(db, SenderAccountCreate(email="owner@mail.ru", password="first"), app_settings)
    account.verification_status = "verified"
    db.commit()
    update_sender_account(db, account, SenderAccountUpdate(password="second"), app_settings)
    assert account.verification_status == "unverified"
    assert CredentialCipher(app_settings.mail_credentials_encryption_key).decrypt(account.encrypted_password) == "second"
    assert "second" not in str(sender_account_to_dict(account))


def test_connection_verification_uses_login_only(db):
    app_settings = settings()
    account = create_sender_account(db, SenderAccountCreate(email="owner@mail.ru", password="secret"), app_settings)
    calls = []

    class VerifyOnly:
        def __init__(self, _account, password, **_): assert password == "secret"
        def verify(self): calls.append("verify")

    verify_sender_account(db, account, app_settings, smtp_client_factory=VerifyOnly)
    assert calls == ["verify"]
    assert account.verification_status == "verified"


@pytest.mark.parametrize("imap_enabled", [False, True])
def test_successful_verification_clears_round_block_and_preserves_accounting(db, imap_enabled):
    app_settings = settings()
    account = create_sender_account(
        db,
        SenderAccountCreate(email="owner@mail.ru", password="secret", imap_enabled=imap_enabled),
        app_settings,
    )
    account.blocked_until_round = 26
    account.block_reason = "Предыдущее временное ограничение"
    account.verification_status = "temporary_error"
    account.verification_error = account.block_reason
    account.sent_today = 17
    account.sent_today_date = datetime.now(timezone.utc).date()
    account.successful_full_batches = 4
    account.current_batch_size = 7
    db.commit()
    accounting = (account.sent_today, account.sent_today_date, account.successful_full_batches, account.current_batch_size)
    calls = []

    class SMTPVerifyOnly:
        def __init__(self, *_args, **_kwargs): pass
        def verify(self): calls.append("smtp")

    class IMAPVerifyOnly:
        def __init__(self, *_args, **_kwargs): pass
        def __enter__(self): calls.append("imap"); return self
        def __exit__(self, *_args): pass

    verify_sender_account(
        db, account, app_settings,
        smtp_client_factory=SMTPVerifyOnly,
        imap_client_factory=IMAPVerifyOnly,
    )
    db.expire_all()
    assert calls == (["smtp", "imap"] if imap_enabled else ["smtp"])
    assert account.verification_status == "verified"
    assert account.verification_error is None
    assert account.blocked_until_round is None
    assert account.block_reason is None
    assert (account.sent_today, account.sent_today_date, account.successful_full_batches, account.current_batch_size) == accounting


@pytest.mark.parametrize("category,reason", [
    ("policy", "DATA: SMTP 550 policy refusal"),
    ("provider", "DATA: SMTP 550 spam message rejected"),
    ("provider", "DATA: SMTP 550 Spam Message Discarded"),
])
@pytest.mark.parametrize("preserve_round_block", [False, True])
@pytest.mark.parametrize("imap_failure", [False, True])
def test_successful_login_retains_policy_hold_and_updates_imap(db, category, reason, preserve_round_block, imap_failure):
    from datetime import timedelta

    app_settings = settings()
    account = create_sender_account(
        db, SenderAccountCreate(email="owner@mail.ru", password="secret", imap_enabled=True), app_settings,
    )
    checked_at = datetime.now(timezone.utc) - timedelta(hours=1)
    account.verification_status = "failed"
    account.verification_error_category = category
    account.verification_error = reason
    account.verification_checked_at = checked_at
    account.blocked_until_round = 6
    account.block_reason = reason
    account.sent_today = 17
    account.imap_verification_status = "temporary_error"
    account.imap_verification_error = "Previous IMAP timeout"
    db.commit()
    calls = []

    class SMTPVerifyOnly:
        def __init__(self, *_args, **_kwargs): pass
        def verify(self): calls.append("smtp")

    class IMAPVerifyOnly:
        def __init__(self, *_args, **_kwargs): pass
        def __enter__(self):
            calls.append("imap")
            if imap_failure:
                raise IMAPCollectorError("Current IMAP timeout", category="timeout")
            return self
        def __exit__(self, *_args): pass

    verify_sender_account(
        db, account, app_settings,
        smtp_client_factory=SMTPVerifyOnly,
        imap_client_factory=IMAPVerifyOnly,
        preserve_round_block=preserve_round_block,
    )
    db.expire_all()
    assert calls == ["smtp", "imap"]
    assert account.verification_status == "failed"
    assert account.verification_error_category == category
    assert account.verification_error == reason
    assert account.verification_retry_at is None
    assert (account.blocked_until_round, account.block_reason, account.sent_today) == (6, reason, 17)
    assert account.verification_checked_at.replace(tzinfo=None) > checked_at.replace(tzinfo=None)
    assert account.imap_verification_status == ("temporary_error" if imap_failure else "verified")
    assert account.imap_verification_error == ("Current IMAP timeout" if imap_failure else None)
    assert account.imap_verification_checked_at is not None


def test_successful_login_clears_nonpolicy_provider_failure(db):
    app_settings = settings()
    account = create_sender_account(db, SenderAccountCreate(email="owner@mail.ru", password="secret"), app_settings)
    account.verification_status = "failed"
    account.verification_error_category = "provider"
    account.verification_error = "SMTP-server temporarily did not support AUTH"
    account.blocked_until_round = 6
    account.block_reason = account.verification_error
    db.commit()

    class SMTPVerifyOnly:
        def __init__(self, *_args, **_kwargs): pass
        def verify(self): pass

    verify_sender_account(db, account, app_settings, smtp_client_factory=SMTPVerifyOnly)
    assert account.verification_status == "verified"
    assert account.verification_error_category is None
    assert account.verification_error is None
    assert account.blocked_until_round is None
    assert account.block_reason is None


@pytest.mark.parametrize("failure", ["temporary", "auth"])
def test_failed_verification_preserves_round_block(db, failure):
    app_settings = settings()
    account = create_sender_account(
        db,
        SenderAccountCreate(email="owner@mail.ru", password="secret", imap_enabled=failure == "imap"),
        app_settings,
    )
    account.blocked_until_round = 26
    account.block_reason = "Предыдущее временное ограничение"
    db.commit()

    class SMTPVerification:
        def __init__(self, *_args, **_kwargs): pass
        def verify(self):
            if failure != "imap":
                raise SMTPDeliveryError("Проверка отклонена", category=failure)

    class FailedIMAPVerification:
        def __init__(self, *_args, **_kwargs): pass
        def __enter__(self): raise IMAPCollectorError("Проверка отклонена")
        def __exit__(self, *_args): pass

    verify_sender_account(
        db, account, app_settings,
        smtp_client_factory=SMTPVerification,
        imap_client_factory=FailedIMAPVerification,
    )
    db.expire_all()
    assert account.verification_status == {"temporary": "temporary_error", "auth": "blocked"}[failure]
    assert account.blocked_until_round == 26
    assert account.block_reason == "Предыдущее временное ограничение"


def test_test_message_sends_exactly_once_to_confirmed_address(db):
    app_settings = settings()
    account = create_sender_account(db, SenderAccountCreate(email="owner@mail.ru", password="secret"), app_settings)
    calls = []

    class FakeClient:
        def __init__(self, *_args, **_kwargs): pass
        def send(self, recipient, subject, body):
            calls.append((recipient, subject, body))
            return SMTPAccepted("<test@mail.ru>", "250", "OK")

    result = send_test_message(account, "manual@example.ru", app_settings, smtp_client_factory=FakeClient)
    assert result.message_id == "<test@mail.ru>"
    assert len(calls) == 1
    assert calls[0][0] == "manual@example.ru"


@pytest.fixture
def policy_held_sender(db):
    app_settings = settings()
    account = create_sender_account(db, SenderAccountCreate(email="owner@mail.ru", password="secret", imap_enabled=True), app_settings)
    account.verification_status = "failed"
    account.verification_error_category = "policy"
    account.verification_error = "DATA: SMTP 550 spam message rejected"
    account.verification_checked_at = datetime.now(timezone.utc) - timedelta(hours=1)
    account.verification_retry_at = account.verification_checked_at + timedelta(minutes=5)
    account.blocked_until_round = 6
    account.block_reason = account.verification_error
    account.imap_verification_status = "temporary_error"
    account.imap_verification_error = "Previous IMAP timeout"
    account.imap_verification_checked_at = account.verification_checked_at
    account.sent_today = 17
    account.sent_today_date = date(2026, 9, 22)
    account.successful_full_batches = 4
    account.current_batch_size = 7
    account.last_sent_at = account.verification_checked_at
    db.commit()
    db.refresh(account)
    return app_settings, account


@pytest.mark.parametrize("category", ["policy", "provider"])
@pytest.mark.parametrize("sent_copy_saved", [False, True])
def test_accepted_test_message_repairs_old_smtp_hold_without_changing_imap_or_accounting(db, policy_held_sender, category, sent_copy_saved):
    app_settings, account = policy_held_sender
    account.verification_error_category = category
    db.commit()
    db.refresh(account)
    old_checked_at = account.verification_checked_at
    unchanged_fields = ("imap_verification_status", "imap_verification_error", "imap_verification_checked_at", "daily_limit", "sent_today", "sent_today_date", "successful_full_batches", "current_batch_size", "last_sent_at")
    before = tuple(getattr(account, field) for field in unchanged_fields)
    calls = []
    accepted = SMTPAccepted("<test@mail.ru>", "250", "OK", sent_copy_saved=sent_copy_saved)

    class SMTP:
        def __init__(self, *_args, **_kwargs): pass
        def send(self, recipient, subject, body):
            assert account.verification_status == "failed"
            calls.append((recipient, subject, body))
            return accepted

    result = send_test_message_and_reconcile(
        db, account, "manual@example.ru", app_settings, smtp_client_factory=SMTP,
        subject="Проверка", body="Тестовое письмо",
    )
    db.refresh(account)

    assert result is accepted
    assert calls == [("manual@example.ru", "Проверка", "Тестовое письмо")]
    assert account.verification_status == "verified"
    assert account.verification_error is None
    assert account.verification_error_category is None
    assert account.verification_retry_at is None
    assert account.blocked_until_round is None
    assert account.block_reason is None
    assert account.verification_checked_at > old_checked_at
    assert tuple(getattr(account, field) for field in unchanged_fields) == before


@pytest.mark.parametrize("category,uncertain", [("policy", False), ("auth", False), ("temporary", False), ("uncertain", True)])
def test_failed_or_uncertain_test_message_never_repairs_sender(db, policy_held_sender, category, uncertain):
    app_settings, account = policy_held_sender
    before = {column.name: getattr(account, column.name) for column in account.__table__.columns}
    error = SMTPDeliveryError("Тест не принят", category=category, uncertain=uncertain)
    calls = []

    class SMTP:
        def __init__(self, *_args, **_kwargs): pass
        def send(self, *_args):
            calls.append("send")
            raise error

    with pytest.raises(SMTPDeliveryError) as caught:
        send_test_message_and_reconcile(db, account, "manual@example.ru", app_settings, smtp_client_factory=SMTP)
    db.refresh(account)

    assert caught.value is error
    assert calls == ["send"]
    assert {column.name: getattr(account, column.name) for column in account.__table__.columns} == before


@pytest.mark.parametrize("change", ["password", "smtp_host", "smtp_error", "smtp_check"])
def test_accepted_test_message_does_not_overwrite_concurrent_credentials_or_smtp_state(db, policy_held_sender, change):
    app_settings, account = policy_held_sender
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    concurrent_state = {}

    class SMTP:
        def __init__(self, *_args, **_kwargs): pass
        def send(self, *_args):
            with factory() as other_db:
                updated = other_db.get(SenderAccount, account.id)
                if change == "password":
                    update_sender_account(other_db, updated, SenderAccountUpdate(password="replacement"), app_settings)
                elif change == "smtp_host":
                    updated.smtp_host = "replacement.example.ru"
                elif change == "smtp_error":
                    updated.verification_error = "Newer SMTP policy refusal"
                    updated.block_reason = updated.verification_error
                    updated.blocked_until_round = 9
                else:
                    updated.verification_checked_at = datetime.now(timezone.utc)
                other_db.commit()
                other_db.refresh(updated)
                concurrent_state.update({column.name: getattr(updated, column.name) for column in updated.__table__.columns})
            return SMTPAccepted("<test@mail.ru>", "250", "OK")

    result = send_test_message_and_reconcile(db, account, "manual@example.ru", app_settings, smtp_client_factory=SMTP)
    db.refresh(account)

    assert result.message_id == "<test@mail.ru>"
    assert {column.name: getattr(account, column.name) for column in account.__table__.columns} == concurrent_state


def test_imap_activity_during_test_message_does_not_prevent_smtp_repair(db, policy_held_sender):
    app_settings, account = policy_held_sender
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    imap_checked_at = datetime.now(timezone.utc)

    class SMTP:
        def __init__(self, *_args, **_kwargs): pass
        def send(self, *_args):
            with factory() as other_db:
                updated = other_db.get(SenderAccount, account.id)
                updated.imap_verification_status = "verified"
                updated.imap_verification_error = None
                updated.imap_verification_checked_at = imap_checked_at
                updated.updated_at = imap_checked_at
                other_db.commit()
            return SMTPAccepted("<test@mail.ru>", "250", "OK")

    send_test_message_and_reconcile(db, account, "manual@example.ru", app_settings, smtp_client_factory=SMTP)
    db.refresh(account)

    assert account.verification_status == "verified"
    assert account.verification_error is None
    assert account.block_reason is None
    assert account.imap_verification_status == "verified"
    assert account.imap_verification_error is None
    assert account.imap_verification_checked_at == imap_checked_at.replace(tzinfo=None)


def test_test_email_api_repairs_sender_and_preserves_stopped_campaign_evidence(db, policy_held_sender, monkeypatch):
    from app import main
    from app.services import sender_accounts

    app_settings, account = policy_held_sender
    campaign = OutreachCampaign(status="stopped", daily_limit=50, hourly_limit=0, min_interval_seconds=60, max_per_domain_per_day=0, sender_account_ids=[account.id], pause_reason="Остановлено пользователем")
    delivery = OutreachDelivery(
        campaign=campaign, sender_account_id=account.id, company_name="Компания", company_inn="1234567890",
        recipient="lead@example.ru", recipient_domain="example.ru", subject="Предложение", body="Текст",
        status="cancelled", smtp_code="550", smtp_response="spam message rejected", error_message="DATA refusal",
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    db.refresh(delivery)
    before_campaign = {column.name: getattr(campaign, column.name) for column in campaign.__table__.columns}
    before_delivery = {column.name: getattr(delivery, column.name) for column in delivery.__table__.columns}
    calls = []

    def accepted_test(sender, recipient, _settings, **kwargs):
        calls.append((sender.id, recipient, kwargs["subject"], kwargs["body"]))
        return SMTPAccepted("<test@mail.ru>", "250", "OK", sent_copy_saved=True)

    monkeypatch.setattr(sender_accounts, "send_test_message", accepted_test)
    result = main.send_mailbox_test_email(
        account.id, SenderTestEmailRequest(recipient="manual@example.ru", confirmed=True, subject="Проверка", body="Тест"), db, app_settings,
    )
    db.refresh(account)
    db.refresh(campaign)
    db.refresh(delivery)

    assert result["accepted"] is True
    assert result["sent_copy_saved"] is True
    assert result["message_id"] == "<test@mail.ru>"
    assert calls == [(account.id, "manual@example.ru", "Проверка", "Тест")]
    assert account.verification_status == "verified"
    assert account.verification_error_category is None
    assert {column.name: getattr(campaign, column.name) for column in campaign.__table__.columns} == before_campaign
    assert {column.name: getattr(delivery, column.name) for column in delivery.__table__.columns} == before_delivery


def test_sender_cannot_be_deleted_when_snapshotted_by_active_campaign(db):
    account = SenderAccount(email="owner@mail.ru", encrypted_password="cipher", verification_status="verified")
    db.add(account)
    db.commit()
    db.add(OutreachCampaign(status="paused", filters={}, daily_limit=50, hourly_limit=0, min_interval_seconds=60, max_per_domain_per_day=0, sender_account_ids=[account.id]))
    db.commit()
    assert sender_used_by_active_campaign(db, account.id) is True


def test_batch_size_schedule():
    assert [batch_size_for_successes(value) for value in (0, 1, 2, 3, 4, 5, 14, 30)] == [5, 5, 6, 6, 7, 7, 12, 12]


@pytest.mark.parametrize("category", ["timeout", "auth", "tls"])
def test_imap_failure_does_not_disable_working_smtp(db, category):
    s = settings()
    a = create_sender_account(db, SenderAccountCreate(email="owner@mail.ru", password="secret", imap_enabled=True), s)
    class SMTP:
        def __init__(self, *_, **__): pass
        def verify(self): pass
    class IMAP:
        def __init__(self, *_, **__): pass
        def __enter__(self): raise IMAPCollectorError("IMAP failed", category=category)
        def __exit__(self, *_): pass
    verify_sender_account(db, a, s, smtp_client_factory=SMTP, imap_client_factory=IMAP)
    assert a.verification_status == "verified"
    assert a.imap_verification_status == ("failed" if category == "auth" else "temporary_error")
    assert a.imap_verification_error == "IMAP failed"


def test_slow_verification_does_not_overwrite_replaced_password(db):
    s = settings()
    a = create_sender_account(db, SenderAccountCreate(email="owner@mail.ru", password="old"), s)
    class SMTP:
        def __init__(self, *_, **__): pass
        def verify(self): update_sender_account(db, a, SenderAccountUpdate(password="new"), s)
    verify_sender_account(db, a, s, smtp_client_factory=SMTP)
    assert a.verification_status == "unverified"
    assert CredentialCipher(s.mail_credentials_encryption_key).decrypt(a.encrypted_password) == "new"


def test_imap_poll_during_smtp_check_does_not_discard_smtp_result(db):
    from datetime import timedelta
    s = settings()
    a = create_sender_account(db, SenderAccountCreate(email="owner@mail.ru", password="secret", imap_enabled=True), s)
    a.verification_status = "temporary_error"
    db.commit()
    newer_imap_check = datetime.now(timezone.utc) + timedelta(seconds=1)

    class SMTP:
        def __init__(self, *_, **__): pass
        def verify(self):
            a.imap_verification_status = "verified"
            a.imap_verification_checked_at = newer_imap_check
            a.updated_at = newer_imap_check
            db.commit()

    class IMAP:
        def __init__(self, *_, **__): pass
        def __enter__(self): raise IMAPCollectorError("stale IMAP failure", category="auth")
        def __exit__(self, *_): pass

    verify_sender_account(db, a, s, smtp_client_factory=SMTP, imap_client_factory=IMAP)
    assert a.verification_status == "verified"
    assert a.imap_verification_status == "verified"
    assert a.imap_verification_error is None
    assert a.imap_verification_checked_at.replace(tzinfo=None) == newer_imap_check.replace(tzinfo=None)


@pytest.mark.parametrize("status,category", [("blocked", "auth"), ("failed", "policy")])
def test_newer_smtp_failure_wins_over_slow_successful_check(db, status, category):
    s = settings()
    a = create_sender_account(db, SenderAccountCreate(email="owner@mail.ru", password="secret"), s)

    class SMTP:
        def __init__(self, *_, **__): pass
        def verify(self):
            a.verification_status = status
            a.verification_error_category = category
            a.verification_error = "newer send failure"
            a.blocked_until_round = 10
            db.commit()

    verify_sender_account(db, a, s, smtp_client_factory=SMTP)
    assert a.verification_status == status
    assert a.verification_error_category == category
    assert a.verification_error == "newer send failure"
    assert a.blocked_until_round == 10


@pytest.mark.parametrize("campaign_status", ["running", "cooldown", "paused", "interrupted"])
def test_health_check_preserves_campaign_and_accounting(db, campaign_status):
    from contextlib import nullcontext
    from datetime import timedelta
    from app.services.sender_accounts import recover_temporary_sender_accounts
    s = settings()
    a = create_sender_account(db, SenderAccountCreate(email="owner@mail.ru", password="secret"), s)
    a.verification_status = "temporary_error"
    a.verification_error_category = "timeout"
    a.verification_retry_at = None  # Legacy temporary errors need recovery too.
    a.verification_checked_at = datetime.now(timezone.utc) - timedelta(hours=1)
    a.blocked_until_round = 19
    a.sent_today, a.successful_full_batches = 12, 4
    rest = datetime.now(timezone.utc) + timedelta(hours=1)
    c = OutreachCampaign(status=campaign_status, filters={}, daily_limit=50, hourly_limit=0, min_interval_seconds=60, max_per_domain_per_day=0, sender_account_ids=[a.id], current_round=16, next_send_at=rest, round_rest_until=rest)
    db.add(c)
    db.commit()

    class SMTP:
        def __init__(self, *_, **__): pass
        def verify(self): pass

    recover_temporary_sender_accounts(s, session_factory=lambda: nullcontext(db), smtp_client_factory=SMTP)
    assert a.verification_status == "verified"
    assert (a.blocked_until_round, a.sent_today, a.successful_full_batches) == (19, 12, 4)
    assert (c.status, c.current_round, c.next_send_at, c.round_rest_until) == (campaign_status, 16, rest, rest)


def test_health_check_failure_does_not_starve_other_accounts(db):
    from contextlib import nullcontext
    from datetime import timedelta
    from app.services.sender_accounts import recover_temporary_sender_accounts
    s = settings()
    accounts = [create_sender_account(db, SenderAccountCreate(email=f"owner{n}@mail.ru", password="secret"), s) for n in range(2)]
    for a in accounts:
        a.verification_status = "temporary_error"
        a.verification_error_category = "timeout"
        a.verification_retry_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()

    class SMTP:
        def __init__(self, account, *_, **__): self.account = account
        def verify(self):
            if self.account.id == accounts[0].id: raise RuntimeError("unexpected failure")

    recover_temporary_sender_accounts(s, session_factory=lambda: nullcontext(db), smtp_client_factory=SMTP)
    assert accounts[0].verification_status == "temporary_error"
    assert accounts[1].verification_status == "verified"


@pytest.mark.parametrize("active,category,due,is_active,expected", [(False,"connection",True,True,True), (True,"connection",True,True,True), (False,"auth",True,True,False), (False,"provider",True,True,False), (False,"connection",False,True,False), (False,"connection",True,False,False)])
def test_background_recovery_checks_due_accounts_and_preserves_campaign_rest(db, active, category, due, is_active, expected):
    from contextlib import nullcontext
    from datetime import timedelta
    from app.services.sender_accounts import recover_temporary_sender_accounts
    s = settings()
    a = create_sender_account(db, SenderAccountCreate(email="owner@mail.ru", password="secret"), s)
    a.verification_status = "temporary_error"
    a.verification_error_category = category
    a.verification_retry_at = datetime.now(timezone.utc) + timedelta(minutes=-1 if due else 10)
    a.blocked_until_round = 17
    a.sent_today = 23
    a.is_active = is_active
    if active:
        db.add(OutreachCampaign(status="paused", filters={}, daily_limit=50, hourly_limit=0, min_interval_seconds=60, max_per_domain_per_day=0, sender_account_ids=[a.id]))
    db.commit()
    calls = []
    class SMTP:
        def __init__(self, *_, **__): pass
        def verify(self): calls.append("login")
    recover_temporary_sender_accounts(s, session_factory=lambda: nullcontext(db), smtp_client_factory=SMTP)
    assert bool(calls) is expected
    assert a.sent_today == 23
    assert (a.verification_status == "verified") is expected
    assert a.blocked_until_round == (None if expected and not active else 17)


@pytest.mark.parametrize("value", ["пароль", "a b", "\n\t ", "ab\x00cd", "ab\x7fcd"])
def test_invalid_password_format_is_rejected_without_value_in_message(value):
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as caught:
        SenderAccountCreate(email="owner@mail.ru", password=value)
    assert value not in caught.value.errors()[0]["msg"]


def test_password_copy_trims_only_edges_and_hides_model_repr():
    data = SenderAccountUpdate(password=" app-password\n")
    assert data.password == "app-password"
    assert "app-password" not in repr(data)


def test_api_password_save_checks_connection_automatically(db, monkeypatch):
    from app import main
    s = settings()
    a = create_sender_account(db, SenderAccountCreate(email="owner@mail.ru", password="old"), s)
    calls = []
    def verify(session, account, settings):
        calls.append(account.id)
        assert CredentialCipher(settings.mail_credentials_encryption_key).decrypt(account.encrypted_password) == "new"
        account.verification_status = "verified"
        session.commit()
        return account
    monkeypatch.setattr(main, "verify_sender_account", verify)
    result = main.patch_sender_account(a.id, SenderAccountUpdate(password="new"), db, s)
    assert calls == [a.id]
    assert result["verification_status"] == "verified"
    assert "encrypted_password" not in result


def test_custom_test_message_content_uses_existing_smtp_client():
    from app.services.sender_accounts import send_test_message
    from app.services.credentials import CredentialCipher, generate_encryption_key
    from app.config import Settings
    from app.models import SenderAccount
    from app.services.smtp import SMTPAccepted
    key = generate_encryption_key()
    settings = Settings(_env_file=None, mail_credentials_encryption_key=key)
    account = SenderAccount(email="sender@mail.ru", is_active=True, smtp_enabled=True,
                            encrypted_password=CredentialCipher(key).encrypt("fake"))
    class FakeSender:
        def __init__(self, *args, **kwargs): pass
        def send(self, recipient, subject, body):
            assert (recipient, subject, body) == ("target@example.ru", "Проверка", "Тест из сервиса")
            return SMTPAccepted("<test@mail.ru>", "250", "OK")
    assert send_test_message(account, "target@example.ru", settings, smtp_client_factory=FakeSender,
                             subject="Проверка", body="Тест из сервиса").message_id == "<test@mail.ru>"
