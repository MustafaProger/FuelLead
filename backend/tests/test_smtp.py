import smtplib

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
