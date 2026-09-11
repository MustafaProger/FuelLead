from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.models import OutreachCampaign, SenderAccount
from app.schemas import SenderAccountCreate, SenderAccountUpdate
from app.services.credentials import CredentialCipher, generate_encryption_key
from app.services.sender_accounts import (
    SenderAccountError,
    batch_size_for_successes,
    create_sender_account,
    send_test_message,
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
    assert a.imap_verification_status == ("temporary_error" if category == "timeout" else "failed")
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


@pytest.mark.parametrize("active,category,due,is_active,expected", [(False,"connection",True,True,True), (True,"connection",True,True,False), (False,"auth",True,True,False), (False,"provider",True,True,False), (False,"connection",False,True,False), (False,"connection",True,False,False)])
def test_background_recovery_only_checks_due_temporary_accounts_without_campaign(db, active, category, due, is_active, expected):
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
    assert a.blocked_until_round == (None if expected else 17)


@pytest.mark.parametrize("value", ["пароль", "a b", "\n\t ", "ab\x00cd"])
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
