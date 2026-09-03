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
from app.services.smtp import SMTPAccepted


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
