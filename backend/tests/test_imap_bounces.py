import asyncio
import imaplib
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.models import Company, CompanyEmail, EmailSuppression, ImapProcessedMessage, OutreachCampaign, OutreachDelivery, SenderAccount
from app.services.credentials import CredentialCipher, generate_encryption_key
from app.services.dsn import parse_permanent_dsn
from app.services.imap_bounces import IMAPCollectorError, MailruIMAPClient, apply_dsn_bounce, process_imap_tick


RAW_DSN = b"""From: postmaster@mail.ru\r
To: sender@mail.ru\r
Message-ID: <bounce-1@mail.ru>\r
Content-Type: multipart/report; report-type=delivery-status; boundary=dsn\r
\r
--dsn\r
Content-Type: text/plain; charset=utf-8\r
\r
Delivery failed.\r
--dsn\r
Content-Type: message/delivery-status\r
\r
Final-Recipient: rfc822; bad@example.ru\r
Action: failed\r
Status: 5.2.1\r
Diagnostic-Code: smtp; mailbox disabled\r
Original-Message-ID: <outbound@mail.ru>\r
\r
--dsn\r
Content-Type: message/rfc822\r
\r
X-FuelLead-Delivery-ID: 1\r
Message-ID: <outbound@mail.ru>\r
\r
--dsn--\r
"""


class FakeIMAPTransport:
    instances = []

    def __init__(self, host, port, *, ssl_context, timeout):
        assert host == "imap.mail.ru"
        assert port == 993
        assert ssl_context.check_hostname is True
        assert timeout == 30
        self.commands = []
        self.__class__.instances.append(self)

    def login(self, email, password):
        self.commands.append(("LOGIN", email, password))

    def select(self, mailbox, readonly):
        self.commands.append(("SELECT", mailbox, readonly))
        return "OK", []

    def response(self, name):
        return name, [b"1" if name == "UIDVALIDITY" else b"1000"]

    def list(self):
        self.commands.append("LIST")
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Sent) "/" "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"',
        ]

    def append(self, mailbox, flags, sent_at, raw_message):
        self.commands.append(("APPEND", mailbox, flags, sent_at, raw_message))
        return "OK", [b"APPEND completed"]

    def close(self):
        self.commands.append("CLOSE")

    def logout(self):
        self.commands.append("LOGOUT")


def test_sent_copy_uses_server_reported_special_use_folder():
    FakeIMAPTransport.instances = []
    account = SenderAccount(
        email="sender@mail.ru",
        imap_host="imap.mail.ru",
        imap_port=993,
    )
    sent_at = datetime.now(timezone.utc)
    raw_message = b"From: sender@mail.ru\r\nTo: lead@example.ru\r\n\r\nHello"

    with MailruIMAPClient(
        account,
        "secret",
        timeout_seconds=30,
        imap_factory=FakeIMAPTransport,
    ) as client:
        client.append_sent(raw_message, sent_at)

    append = next(command for command in FakeIMAPTransport.instances[0].commands if isinstance(command, tuple) and command[0] == "APPEND")
    assert append[1] == b'"&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"'
    assert append[2] == r"(\Seen)"
    assert append[3] == sent_at
    assert append[4] == raw_message


def test_dsn_classifier_uses_action_and_enhanced_status():
    bounce = parse_permanent_dsn(RAW_DSN)
    assert bounce is not None
    assert bounce.status_code == "5.2.1"
    assert bounce.recipient == "bad@example.ru"
    assert bounce.original_message_id == "<outbound@mail.ru>"


def test_late_imap_bounce_is_idempotent_and_suppresses_address(db):
    account = SenderAccount(email="sender@mail.ru", encrypted_password="cipher", verification_status="verified", imap_enabled=True)
    company = Company(name="Компания", inn="770000000001", status="sent")
    company.emails.append(CompanyEmail(email="bad@example.ru"))
    campaign = OutreachCampaign(status="completed", filters={}, daily_limit=50, hourly_limit=0, min_interval_seconds=60, max_per_domain_per_day=0, accepted_count=1, sent_count=1, recipient_count=1)
    delivery = OutreachDelivery(company_id=None, company_name="Компания", company_inn=company.inn, recipient="bad@example.ru", recipient_domain="example.ru", subject="Тема", body="Текст", status="accepted", message_id="<outbound@mail.ru>", sender_account=account, accepted_at=datetime.now(timezone.utc))
    campaign.deliveries.append(delivery)
    db.add_all([account, company, campaign])
    db.commit()
    delivery.company_id = company.id
    db.commit()

    assert apply_dsn_bounce(db, account, 101, RAW_DSN) is True
    assert apply_dsn_bounce(db, account, 101, RAW_DSN) is False
    db.refresh(delivery)
    db.refresh(campaign)
    assert delivery.status == "bounced"
    assert campaign.accepted_count == 0
    assert campaign.bounced_count == 1
    assert db.query(type(account)).count() == 1
    suppression = db.query(EmailSuppression).one()
    assert suppression.email == "bad@example.ru"
    assert suppression.smtp_code == "5.2.1"
    assert account.imap_last_uid == 101


def test_unrecognized_message_is_not_deleted_or_reprocessed(db):
    account = SenderAccount(email="sender@mail.ru", encrypted_password="cipher", verification_status="verified", imap_enabled=True)
    db.add(account)
    db.commit()
    raw = b"From: friend@example.ru\r\nMessage-ID: <ordinary@example.ru>\r\n\r\nHello"
    assert apply_dsn_bounce(db, account, 7, raw) is False
    assert apply_dsn_bounce(db, account, 7, raw) is False
    assert account.imap_last_uid == 7


def test_failed_imap_login_closes_socket_without_retrying_password():
    import imaplib
    import pytest
    from app.services.imap_bounces import IMAPCollectorError
    class Rejected(FakeIMAPTransport):
        instances = []
        def login(self, email, password): raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        def shutdown(self): self.commands.append("SHUTDOWN")
    account = SenderAccount(email="sender@mail.ru", imap_host="imap.mail.ru", imap_port=993)
    with pytest.raises(IMAPCollectorError) as caught:
        with MailruIMAPClient(account, "secret", timeout_seconds=30, imap_factory=Rejected): pass
    assert caught.value.category == "auth"
    assert len(Rejected.instances) == 1
    assert Rejected.instances[0].commands == ["SHUTDOWN"]


def test_imap_retry_after_connection_failure_and_ignore_old_uid():
    attempts = []
    class Transport(FakeIMAPTransport):
        def uid(self, command, *args):
            if command == "search": return "OK", [b"10 11"]
            assert args == (b"11", "(BODY.PEEK[])")
            return "OK", [(b"11", b"Message body")]
    def factory(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1: raise ConnectionResetError()
        return Transport(*args, **kwargs)
    account = SenderAccount(email="sender@mail.ru", imap_host="imap.mail.ru", imap_port=993)
    with MailruIMAPClient(account, "secret", timeout_seconds=30, imap_factory=factory, sleep_func=lambda _: None) as imap:
        assert imap.messages_after(10, 50) == [(11, b"Message body")]
    assert len(attempts) == 2


def test_imap_read_disconnect_is_safe_and_recoverable():
    import imaplib
    import pytest
    from app.services.imap_bounces import IMAPCollectorError
    class Disconnected(FakeIMAPTransport):
        def uid(self, *_): raise imaplib.IMAP4.abort("private provider details")
    account = SenderAccount(email="sender@mail.ru", imap_host="imap.mail.ru", imap_port=993)
    with MailruIMAPClient(account, "secret", timeout_seconds=30, imap_factory=Disconnected) as imap:
        with pytest.raises(IMAPCollectorError) as caught: imap.messages_after(0, 50)
    assert caught.value.category == "connection"
    assert "private provider details" not in str(caught.value)


def test_sent_copy_and_login_do_not_depend_on_inbox_selection():
    class UnavailableInbox(FakeIMAPTransport):
        instances = []

        def select(self, *_args, **_kwargs):
            pytest.fail("APPEND and authentication checks must not select INBOX")

    account = SenderAccount(email="sender@mail.ru", imap_host="imap.mail.ru", imap_port=993)
    with MailruIMAPClient(account, "secret", timeout_seconds=30, imap_factory=UnavailableInbox) as client:
        client.append_sent(b"Message-ID: <sent@example.ru>\r\n\r\nHello", datetime.now(timezone.utc))
    assert any(isinstance(command, tuple) and command[0] == "APPEND" for command in UnavailableInbox.instances[0].commands)


def test_unavailable_inbox_is_a_recoverable_folder_error():
    class UnavailableInbox(FakeIMAPTransport):
        def select(self, *_args, **_kwargs):
            return "NO", [b"Folder temporarily unavailable"]

    account = SenderAccount(email="sender@mail.ru", imap_host="imap.mail.ru", imap_port=993)
    with MailruIMAPClient(account, "secret", timeout_seconds=30, imap_factory=UnavailableInbox) as client:
        with pytest.raises(IMAPCollectorError) as caught:
            client.messages_after(0, 50)
    assert caught.value.category == "temporary"


@pytest.mark.parametrize("failure", [("NO", [b"Try again"]), ("OK", [b"malformed fetch response"])])
def test_failed_fetch_cannot_advance_past_a_missing_bounce(failure):
    fetched = []

    class MissingBounce(FakeIMAPTransport):
        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"12 11 10 11"]
            uid = args[0]
            fetched.append(uid)
            if uid == b"11":
                return failure
            return "OK", [(uid, b"Message body")]

    account = SenderAccount(email="sender@mail.ru", imap_host="imap.mail.ru", imap_port=993)
    with MailruIMAPClient(account, "secret", timeout_seconds=30, imap_factory=MissingBounce) as client:
        with pytest.raises(IMAPCollectorError) as caught:
            client.messages_after(9, 50)
    assert caught.value.category == "temporary"
    assert fetched == [b"10", b"11"]


def test_expunge_between_search_and_fetch_does_not_block_later_messages():
    class ExpungedMessage(FakeIMAPTransport):
        def uid(self, command, *args):
            if command == "search":
                return "OK", [b"11 12"]
            if args[0] == b"11":
                return "OK", [None]
            return "OK", [(b"12", b"Message body")]

    account = SenderAccount(email="sender@mail.ru", imap_host="imap.mail.ru", imap_port=993)
    with MailruIMAPClient(account, "secret", timeout_seconds=30, imap_factory=ExpungedMessage) as client:
        assert client.messages_after(10, 50) == [(12, b"Message body")]


def test_logout_failure_still_closes_imap_socket():
    class FailedLogout(FakeIMAPTransport):
        instances = []

        def logout(self):
            raise imaplib.IMAP4.abort("Server disconnected")

        def shutdown(self):
            self.commands.append("SHUTDOWN")

    account = SenderAccount(email="sender@mail.ru", imap_host="imap.mail.ru", imap_port=993)
    with MailruIMAPClient(account, "secret", timeout_seconds=30, imap_factory=FailedLogout):
        pass
    assert FailedLogout.instances[0].commands[-1] == "SHUTDOWN"


@pytest.mark.parametrize("category", ["temporary", "connection", "timeout", "protocol", "tls", "auth"])
def test_worker_recovers_non_auth_failure_without_password_replacement(db, category):
    app_settings = Settings(_env_file=None, mail_credentials_encryption_key=generate_encryption_key())
    account = SenderAccount(email="sender@mail.ru", encrypted_password=CredentialCipher(app_settings.mail_credentials_encryption_key).encrypt("secret"), imap_enabled=True)
    db.add(account)
    db.commit()
    calls = []

    class Check:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            calls.append("login")
            if len(calls) == 1:
                raise IMAPCollectorError("Safe failure", category=category)
            return self

        def __exit__(self, *_args):
            pass

        def messages_after(self, *_args):
            return []

    factory = sessionmaker(bind=db.get_bind())
    process_imap_tick(app_settings, session_factory=factory, client_factory=Check)
    db.refresh(account)
    assert account.imap_verification_status == ("failed" if category == "auth" else "temporary_error")
    assert account.imap_verification_error == "Safe failure"
    process_imap_tick(app_settings, session_factory=factory, client_factory=Check)
    db.refresh(account)
    assert len(calls) == (1 if category == "auth" else 2)
    assert account.imap_verification_status == ("failed" if category == "auth" else "verified")
    assert account.imap_verification_error == ("Safe failure" if category == "auth" else None)


@pytest.mark.parametrize("failed", [False, True])
def test_worker_old_password_result_does_not_overwrite_replacement(db, failed):
    app_settings = Settings(_env_file=None, mail_credentials_encryption_key=generate_encryption_key())
    cipher = CredentialCipher(app_settings.mail_credentials_encryption_key)
    account = SenderAccount(email="sender@mail.ru", encrypted_password=cipher.encrypt("old-secret"), imap_enabled=True)
    db.add(account)
    db.commit()
    replacement = cipher.encrypt("new-secret")

    class PasswordChangedDuringLogin:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            account.encrypted_password = replacement
            account.imap_verification_status = "verified"
            account.imap_verification_error = None
            db.commit()
            if failed:
                raise IMAPCollectorError("Old password rejected", category="auth")
            return self

        def __exit__(self, *_args):
            pass

        def messages_after(self, *_args):
            return [(3, RAW_DSN)]

    process_imap_tick(app_settings, session_factory=sessionmaker(bind=db.get_bind()), client_factory=PasswordChangedDuringLogin)
    db.refresh(account)
    assert account.encrypted_password == replacement
    assert account.imap_verification_status == "verified"
    assert account.imap_verification_error is None
    assert account.imap_last_uid == 0


def test_unexpected_account_error_does_not_starve_other_mailboxes(db):
    app_settings = Settings(_env_file=None, mail_credentials_encryption_key=generate_encryption_key())
    password = CredentialCipher(app_settings.mail_credentials_encryption_key).encrypt("secret")
    broken = SenderAccount(email="broken@mail.ru", encrypted_password=password, imap_enabled=True)
    healthy = SenderAccount(email="healthy@mail.ru", encrypted_password=password, imap_enabled=True)
    db.add_all([broken, healthy])
    db.commit()

    class Check:
        def __init__(self, account, *_args, **_kwargs):
            self.account = account

        def __enter__(self):
            if self.account.email == "broken@mail.ru":
                raise RuntimeError("Unexpected client bug")
            return self

        def __exit__(self, *_args):
            pass

        def messages_after(self, *_args):
            return []

    process_imap_tick(app_settings, session_factory=sessionmaker(bind=db.get_bind()), client_factory=Check)
    db.refresh(healthy)
    assert healthy.imap_verification_status == "verified"


def test_smtp_recovery_exception_does_not_skip_imap_collection(monkeypatch):
    from app.services import imap_bounces, sender_accounts

    calls = []

    def fail_recovery(_settings):
        calls.append("smtp")
        raise RuntimeError("Unexpected recovery failure")

    async def finish_after_tick(_seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(sender_accounts, "recover_temporary_sender_accounts", fail_recovery)
    monkeypatch.setattr(imap_bounces, "process_imap_tick", lambda _settings: calls.append("imap"))
    monkeypatch.setattr(imap_bounces, "process_reply_folders_tick", lambda _settings: calls.append("folders"))
    monkeypatch.setattr(imap_bounces.asyncio, "sleep", finish_after_tick)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(imap_bounces.run_imap_worker(Settings(_env_file=None)))
    assert calls == ["imap", "folders", "smtp"]


@pytest.mark.parametrize("previous,uidnext,expected_start,reset", [
    (123, 2, 1, True),
    (None, 200, 101, False),
    (None, 2, 1, True),
    (124, 200, 101, False),
])
def test_uidvalidity_change_resets_only_rebuilt_inbox_cursor(previous, uidnext, expected_start, reset):
    class Inbox(FakeIMAPTransport):
        def response(self, name):
            return name, [str(124 if name == "UIDVALIDITY" else uidnext).encode()]

        def uid(self, command, *args):
            assert command == "search"
            assert args == (None, f"UID {expected_start}:*")
            return "OK", [b""]

    account = SenderAccount(email="sender@mail.ru", imap_host="imap.mail.ru", imap_port=993, imap_uidvalidity=previous)
    with MailruIMAPClient(account, "secret", timeout_seconds=30, imap_factory=Inbox) as client:
        assert client.messages_after(100, 50) == []
        assert client.uidvalidity == 124
        assert client.uid_namespace_reset is reset


def test_uidvalidity_change_keeps_bounce_history_and_other_mailbox_tracking(db):
    app_settings = Settings(_env_file=None, mail_credentials_encryption_key=generate_encryption_key())
    password = CredentialCipher(app_settings.mail_credentials_encryption_key).encrypt("secret")
    account = SenderAccount(email="sender@mail.ru", encrypted_password=password, imap_enabled=True, imap_uidvalidity=12)
    other = SenderAccount(email="other@mail.ru", encrypted_password=password, imap_enabled=False, imap_last_uid=10)
    campaign = OutreachCampaign(status="completed", filters={}, daily_limit=50, hourly_limit=0, min_interval_seconds=60, max_per_domain_per_day=0, accepted_count=1, sent_count=1, recipient_count=1)
    delivery = OutreachDelivery(company_name="Компания", company_inn="770000000001", recipient="bad@example.ru", recipient_domain="example.ru", subject="Тема", body="Текст", status="accepted", message_id="<outbound@mail.ru>", sender_account=account, accepted_at=datetime.now(timezone.utc))
    campaign.deliveries.append(delivery)
    db.add_all([account, other, campaign])
    db.commit()
    apply_dsn_bounce(db, account, 100, RAW_DSN)
    apply_dsn_bounce(db, other, 10, b"Message-ID: <other@example.ru>\r\n\r\nHello")

    class RebuiltInbox(FakeIMAPTransport):
        def response(self, name):
            return name, [b"13" if name == "UIDVALIDITY" else b"2"]

        def uid(self, command, *args):
            if command == "search":
                assert args == (None, "UID 1:*")
                return "OK", [b"1"]
            return "OK", [(b"1", b"Message-ID: <fresh@example.ru>\r\n\r\nHello")]

    def client_factory(*args, **kwargs):
        return MailruIMAPClient(*args, **kwargs, imap_factory=RebuiltInbox)

    process_imap_tick(app_settings, session_factory=sessionmaker(bind=db.get_bind()), client_factory=client_factory)
    db.expire_all()
    assert account.imap_uidvalidity == 13
    assert account.imap_last_uid == 1
    assert db.query(ImapProcessedMessage).filter_by(sender_account_id=account.id).one().uid == 1
    assert db.query(ImapProcessedMessage).filter_by(sender_account_id=other.id).one().uid == 10
    assert delivery.status == "bounced"
    assert campaign.bounced_count == 1
    assert campaign.accepted_count == 0
    assert db.query(EmailSuppression).one().delivery_id == delivery.id


def test_old_namespace_batch_cannot_overwrite_new_namespace_tracking(db):
    account = SenderAccount(email="sender@mail.ru", imap_uidvalidity=13, imap_last_uid=1)
    db.add(account)
    db.commit()
    assert apply_dsn_bounce(db, account, 100, RAW_DSN, expected_uidvalidity=12) is False
    assert account.imap_uidvalidity == 13
    assert account.imap_last_uid == 1
    assert db.query(ImapProcessedMessage).count() == 0


@pytest.mark.parametrize("deleted", [False, True])
def test_failed_old_check_cannot_overwrite_concurrent_health_check_or_deletion(db, deleted):
    app_settings = Settings(_env_file=None, mail_credentials_encryption_key=generate_encryption_key())
    password = CredentialCipher(app_settings.mail_credentials_encryption_key).encrypt("secret")
    account = SenderAccount(email="sender@mail.ru", encrypted_password=password, imap_enabled=True)
    healthy = SenderAccount(email="healthy@mail.ru", encrypted_password=password, imap_enabled=True)
    db.add_all([account, healthy])
    db.commit()

    class Check:
        def __init__(self, checked_account, *_args, **_kwargs):
            self.email = checked_account.email

        def __enter__(self):
            if self.email == "sender@mail.ru":
                if deleted:
                    db.delete(account)
                else:
                    account.imap_verification_status = "verified"
                    account.imap_verification_checked_at = datetime.now(timezone.utc)
                db.commit()
                raise IMAPCollectorError("Old connection failed", category="connection")
            return self

        def __exit__(self, *_args):
            pass

        def messages_after(self, *_args):
            return []

    process_imap_tick(app_settings, session_factory=sessionmaker(bind=db.get_bind()), client_factory=Check)
    db.refresh(healthy)
    assert healthy.imap_verification_status == "verified"
    if not deleted:
        db.refresh(account)
        assert account.imap_verification_status == "verified"
        assert account.imap_verification_error is None
