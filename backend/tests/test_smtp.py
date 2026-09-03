import smtplib
from email import policy
from email.parser import BytesParser

import pytest

from app.models import SenderAccount
from app.services.smtp import MailruSMTPClient, SMTPDeliveryError


class FakeSMTP:
    instances = []

    def __init__(self, host, port, *, timeout, context):
        assert host == "smtp.mail.ru"
        assert port == 465
        assert timeout == 30
        assert context.check_hostname is True
        self.commands = []
        self.__class__.instances.append(self)

    def ehlo(self): self.commands.append("EHLO")
    def login(self, email, password): self.commands.append(("LOGIN", email, password))
    def mail(self, sender): self.commands.append(("MAIL", sender)); return 250, b"2.1.0 OK"
    def rcpt(self, recipient): self.commands.append(("RCPT", recipient)); return 250, b"2.1.5 OK"
    def data(self, payload): self.commands.append(("DATA", payload)); return 250, b"2.0.0 accepted"
    def quit(self): self.commands.append("QUIT")
    def close(self): self.commands.append("CLOSE")


def account() -> SenderAccount:
    return SenderAccount(email="sender@mail.ru", display_name="FuelLead")


class FakeSentIMAP:
    messages = []

    def __init__(self, account, password, *, timeout_seconds):
        assert account.email == "sender@mail.ru"
        assert password == "secret"
        assert timeout_seconds == 17

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def append_sent(self, raw_message, sent_at):
        self.__class__.messages.append((raw_message, sent_at))


def test_smtp_verify_authenticates_without_sending():
    FakeSMTP.instances = []
    MailruSMTPClient(account(), "secret", smtp_factory=FakeSMTP).verify()
    commands = FakeSMTP.instances[0].commands
    assert commands == ["EHLO", ("LOGIN", "sender@mail.ru", "secret"), "QUIT"]
    assert not any(isinstance(item, tuple) and item[0] in ("MAIL", "RCPT", "DATA") for item in commands)


def test_smtp_send_captures_data_acceptance_and_technical_headers():
    FakeSMTP.instances = []
    result = MailruSMTPClient(account(), "secret", smtp_factory=FakeSMTP).send(
        "lead@example.ru", "Тема", "Текст", delivery_id=7, campaign_id=3
    )
    payload = next(item[1] for item in FakeSMTP.instances[0].commands if isinstance(item, tuple) and item[0] == "DATA")
    assert b"X-FuelLead-Delivery-ID: 7" in payload
    assert b"X-FuelLead-Campaign-ID: 3" in payload
    assert result.smtp_code == "2.0.0"
    assert result.message_id.startswith("<")


def test_accepted_message_is_saved_to_sent_over_imap():
    FakeSMTP.instances = []
    FakeSentIMAP.messages = []
    sender_account = account()
    sender_account.imap_enabled = True

    result = MailruSMTPClient(
        sender_account,
        "secret",
        smtp_factory=FakeSMTP,
        imap_timeout_seconds=17,
        imap_client_factory=FakeSentIMAP,
    ).send("lead@example.ru", "Тема", "Текст")

    assert result.sent_copy_saved is True
    assert result.sent_copy_error is None
    assert len(FakeSentIMAP.messages) == 1
    saved = BytesParser(policy=policy.default).parsebytes(FakeSentIMAP.messages[0][0])
    assert saved["Message-ID"] == result.message_id
    assert saved["To"] == "lead@example.ru"


def test_imap_copy_failure_does_not_turn_accepted_smtp_into_failure():
    class FailingSentIMAP(FakeSentIMAP):
        def append_sent(self, raw_message, sent_at):
            raise RuntimeError("private provider details")

    sender_account = account()
    sender_account.imap_enabled = True
    result = MailruSMTPClient(
        sender_account,
        "secret",
        smtp_factory=FakeSMTP,
        imap_timeout_seconds=17,
        imap_client_factory=FailingSentIMAP,
    ).send("lead@example.ru", "Тема", "Текст")

    assert result.smtp_code == "2.0.0"
    assert result.sent_copy_saved is False
    assert result.sent_copy_error == "Не удалось сохранить копию письма в IMAP Mail.ru"


def test_transient_connection_failure_is_retried_before_smtp_attempt():
    attempts = []

    def flaky_factory(*args, **kwargs):
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise OSError("temporary disconnect")
        return FakeSMTP(*args, **kwargs)

    result = MailruSMTPClient(
        account(),
        "secret",
        smtp_factory=flaky_factory,
        connect_retry_delay_seconds=0,
    ).send("lead@example.ru", "Тема", "Текст")

    assert attempts == [1, 2, 3]
    assert result.smtp_code == "2.0.0"


def test_rcpt_permanent_failure_is_bounce_without_literal_matching():
    class RefusingSMTP(FakeSMTP):
        def rcpt(self, recipient): return 554, b"5.2.1 mailbox disabled"

    with pytest.raises(SMTPDeliveryError) as caught:
        MailruSMTPClient(account(), "secret", smtp_factory=RefusingSMTP).send("lead@example.ru", "Тема", "Текст")
    assert caught.value.permanent_recipient_failure is True
    assert caught.value.smtp_code == "5.2.1"


def test_auth_error_is_safe_and_does_not_include_secret_or_auth_command():
    class AuthFailSMTP(FakeSMTP):
        def login(self, email, password):
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 AUTH secret rejected")

    with pytest.raises(SMTPDeliveryError) as caught:
        MailruSMTPClient(account(), "secret", smtp_factory=AuthFailSMTP).verify()
    assert caught.value.category == "auth"
    assert "secret" not in str(caught.value)
    assert "AUTH" not in str(caught.value)
