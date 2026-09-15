import base64
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

    def ehlo(self): self.commands.append("EHLO"); return 250, b"smtp.mail.ru"
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
    assert result.sent_copy_error == "Не удалось сохранить копию письма в IMAP"


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


def test_wire_message_is_crlf_and_7bit_safe_with_russian_body():
    FakeSMTP.instances = []
    MailruSMTPClient(account(), "secret", smtp_factory=FakeSMTP).send("lead@example.ru", "Тема", "Первая строка\n.Вторая строка\n")
    payload = next(c[1] for c in FakeSMTP.instances[0].commands if isinstance(c, tuple) and c[0] == "DATA")
    assert b"\n" not in payload.replace(b"\r\n", b"")
    assert payload.isascii()
    assert BytesParser(policy=policy.default).parsebytes(payload).get_content().replace("\r\n", "\n") == "Первая строка\n.Вторая строка\n"


@pytest.mark.parametrize("code,reply", [(550, b"spam message rejected"), (554, b"5.7.1 policy refusal"), (450, b"4.2.0 try later")])
def test_rcpt_policy_and_temporary_refusals_do_not_suppress_recipient(code, reply):
    class RefusingSMTP(FakeSMTP):
        def rcpt(self, recipient): return code, reply
    with pytest.raises(SMTPDeliveryError) as caught:
        MailruSMTPClient(account(), "secret", smtp_factory=RefusingSMTP).send("lead@example.ru", "Subject", "Body")
    assert caught.value.permanent_recipient_failure is False
    assert caught.value.smtp_response == reply.decode()
    assert "RCPT" in caught.value.safe_message


@pytest.mark.parametrize("raised", [False, True])
def test_data_refusal_preserves_safe_provider_reason(raised):
    class RefusingSMTP(FakeSMTP):
        def data(self, payload):
            if raised: raise smtplib.SMTPDataError(550, b"spam message rejected password=secret")
            return 550, b"spam message rejected password=secret"
    with pytest.raises(SMTPDeliveryError) as caught:
        MailruSMTPClient(account(), "secret", smtp_factory=RefusingSMTP).send("lead@example.ru", "Subject", "Body")
    assert caught.value.category == "provider"
    assert caught.value.uncertain is False
    assert "spam message rejected" in caught.value.smtp_response
    assert "secret" not in caught.value.safe_message


def test_temporary_auth_error_is_retried_and_not_reported_as_bad_password():
    class TemporaryAuth(FakeSMTP):
        count = 0
        def login(self, email, password):
            self.__class__.count += 1
            raise smtplib.SMTPAuthenticationError(454, b"4.7.0 temporary auth failure")
    with pytest.raises(SMTPDeliveryError) as caught:
        MailruSMTPClient(account(), "secret", smtp_factory=TemporaryAuth, sleep_func=lambda _: None).verify()
    assert TemporaryAuth.count == 3
    assert caught.value.category == "temporary"


def test_permanent_auth_failure_is_not_retried():
    class BadAuth(FakeSMTP):
        count = 0
        def login(self, email, password):
            self.__class__.count += 1
            raise smtplib.SMTPAuthenticationError(535, b"AUTH secret")
    with pytest.raises(SMTPDeliveryError):
        MailruSMTPClient(account(), "secret", smtp_factory=BadAuth).verify()
    assert BadAuth.count == 1


@pytest.mark.parametrize("kind", ["reset", "timeout", "tls", "disconnect"])
def test_every_transport_disconnect_after_data_is_uncertain_and_never_retried(kind):
    import ssl
    errors = {"reset": ConnectionResetError(), "timeout": TimeoutError(), "tls": ssl.SSLEOFError(), "disconnect": smtplib.SMTPServerDisconnected()}
    class LostSMTP(FakeSMTP):
        attempts = 0
        def data(self, payload):
            self.__class__.attempts += 1
            raise errors[kind]
    with pytest.raises(SMTPDeliveryError) as caught:
        MailruSMTPClient(account(), "secret", smtp_factory=LostSMTP).send("lead@example.ru", "Subject", "Body")
    assert caught.value.uncertain is True
    assert LostSMTP.attempts == 1


def test_failed_quit_does_not_undo_accepted_message():
    class LostQuit(FakeSMTP):
        def quit(self): raise ConnectionResetError()
    result = MailruSMTPClient(account(), "secret", smtp_factory=LostQuit).send("lead@example.ru", "Subject", "Body")
    assert result.smtp_code == "2.0.0"


@pytest.mark.parametrize("raised", [False, True])
@pytest.mark.parametrize("recipient,reason", [
    ("shaturadcy@mail.ru", "user not found"),
    ("eskstyle4@mail.ru", "user is terminated"),
])
def test_mailru_data_missing_recipient_does_not_fail_sender(recipient, reason, raised):
    reply = f"Message was not accepted -- invalid mailbox. Local mailbox {recipient} is unavailable: {reason}".encode()

    class MissingRecipient(FakeSMTP):
        def data(self, payload):
            if raised:
                raise smtplib.SMTPDataError(550, reply)
            return 550, reply

    with pytest.raises(SMTPDeliveryError) as caught:
        MailruSMTPClient(account(), "secret", smtp_factory=MissingRecipient).send(recipient, "Subject", "Body")

    assert caught.value.category == "recipient"
    assert caught.value.permanent_recipient_failure is True
    assert caught.value.uncertain is False
    assert caught.value.smtp_response == reply.decode()
    assert str(caught.value).startswith("DATA:")


@pytest.mark.parametrize("reply", [
    b"Message was not accepted -- invalid mailbox",
    b"Local mailbox sender@mail.ru is unavailable: user not found",
    b"Local mailbox other@mail.ru is unavailable: user is terminated",
    b"Local mailbox lead@mail.ru is unavailable: temporarily offline",
    b"Local mailbox prefixlead@mail.ru is unavailable: user not found",
])
def test_mailru_data_refusal_requires_specific_missing_target_evidence(reply):
    class RefusingSMTP(FakeSMTP):
        def data(self, payload):
            return 550, reply

    with pytest.raises(SMTPDeliveryError) as caught:
        MailruSMTPClient(account(), "secret", smtp_factory=RefusingSMTP).send("lead@mail.ru", "Subject", "Body")

    assert caught.value.category == "provider"
    assert caught.value.permanent_recipient_failure is False


@pytest.mark.parametrize("stage", ["rcpt", "data"])
@pytest.mark.parametrize("enhanced", ["5.1.1", "5.1.2", "5.1.3", "5.1.6", "5.2.1"])
def test_explicit_destination_failure_is_recipient_scoped_at_rcpt_or_data(stage, enhanced):
    class RefusingSMTP(FakeSMTP):
        pass
    setattr(RefusingSMTP, stage, lambda *_: (550, f"{enhanced} rejected".encode()))

    with pytest.raises(SMTPDeliveryError) as caught:
        MailruSMTPClient(account(), "secret", smtp_factory=RefusingSMTP).send("lead@example.ru", "Subject", "Body")

    assert caught.value.category == "recipient"
    assert caught.value.permanent_recipient_failure is True
    assert caught.value.uncertain is False


@pytest.mark.parametrize("enhanced,category", [
    ("5.1.7", "provider"),
    ("5.1.8", "provider"),
    ("5.1.0", "provider"),
    ("5.1.5", "provider"),
    ("5.2.2", "recipient"),
    ("4.2.2", "recipient"),
    ("4.2.1", "recipient"),
    ("4.1.1", "recipient"),
    ("5.2.3", "content"),
    ("5.3.4", "content"),
    ("5.6.3", "content"),
])
def test_sender_full_mailbox_and_content_errors_never_suppress_recipient(enhanced, category):
    class RefusingSMTP(FakeSMTP):
        def rcpt(self, recipient):
            return int(enhanced[0]) * 100 + 50, f"{enhanced} rejected".encode()

    with pytest.raises(SMTPDeliveryError) as caught:
        MailruSMTPClient(account(), "secret", smtp_factory=RefusingSMTP).send("lead@example.ru", "Subject", "Body")

    assert caught.value.category == category
    assert caught.value.permanent_recipient_failure is False


def test_mail_from_failure_never_suppresses_recipient():
    class RefusingSMTP(FakeSMTP):
        def mail(self, sender):
            return 550, b"5.1.1 sender unavailable"

    with pytest.raises(SMTPDeliveryError) as caught:
        MailruSMTPClient(account(), "secret", smtp_factory=RefusingSMTP).send("lead@example.ru", "Subject", "Body")

    assert caught.value.permanent_recipient_failure is False
    assert caught.value.category == "provider"


def test_temporary_reply_with_missing_mailbox_text_does_not_suppress_recipient():
    class RefusingSMTP(FakeSMTP):
        def data(self, payload):
            return 450, b"Local mailbox lead@mail.ru is unavailable: user not found"

    with pytest.raises(SMTPDeliveryError) as caught:
        MailruSMTPClient(account(), "secret", smtp_factory=RefusingSMTP).send("lead@mail.ru", "Subject", "Body")

    assert caught.value.category == "temporary"
    assert caught.value.permanent_recipient_failure is False


def test_auth_refusal_preserves_safe_access_reason_and_numeric_code():
    class AuthFailSMTP(FakeSMTP):
        def login(self, email, password):
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Access to this service is disabled; password=secret")

    with pytest.raises(SMTPDeliveryError) as caught:
        MailruSMTPClient(account(), "secret", smtp_factory=AuthFailSMTP).verify()

    assert caught.value.category == "auth"
    assert "Access to this service is disabled" in caught.value.smtp_response
    assert "SMTP 535 / 5.7.8" in caught.value.safe_message
    assert "secret" not in caught.value.smtp_response
    assert "secret" not in caught.value.safe_message


def test_auth_provider_detail_redacts_plain_payload_even_without_auth_prefix():
    payload = base64.b64encode(b"\x00sender@mail.ru\x00secret").decode()

    class AuthFailSMTP(FakeSMTP):
        def login(self, email, password):
            raise smtplib.SMTPAuthenticationError(535, f"5.7.8 Invalid credentials {payload}".encode())

    with pytest.raises(SMTPDeliveryError) as caught:
        MailruSMTPClient(account(), "secret", smtp_factory=AuthFailSMTP).verify()

    assert payload not in caught.value.smtp_response
    assert payload not in caught.value.safe_message
    assert "Invalid credentials" in caught.value.smtp_response


@pytest.mark.parametrize("code,category,attempts", [(421, "temporary", 3), (550, "provider", 1)])
def test_rejected_ehlo_stops_before_sending_credentials(code, category, attempts):
    class BadGreeting(FakeSMTP):
        instances = []
        def ehlo(self):
            return code, b"server unavailable"

    with pytest.raises(SMTPDeliveryError) as caught:
        MailruSMTPClient(account(), "secret", smtp_factory=BadGreeting, sleep_func=lambda _: None).verify()

    assert caught.value.category == category
    assert len(BadGreeting.instances) == attempts
    assert all(instance.commands == ["CLOSE"] for instance in BadGreeting.instances)


def test_missing_auth_capability_is_configuration_error_without_retries():
    class NoAuthSMTP(FakeSMTP):
        instances = []
        def login(self, email, password):
            raise smtplib.SMTPNotSupportedError("SMTP AUTH extension not supported by server")

    with pytest.raises(SMTPDeliveryError) as caught:
        MailruSMTPClient(account(), "secret", smtp_factory=NoAuthSMTP, sleep_func=lambda _: None).verify()

    assert caught.value.category == "provider"
    assert len(NoAuthSMTP.instances) == 1
    assert "порт 465" in caught.value.safe_message


def test_cleanup_failure_does_not_undo_successful_auth_verification():
    class BrokenCleanup(FakeSMTP):
        def quit(self):
            raise ConnectionResetError()
        def close(self):
            raise OSError("already closed")

    MailruSMTPClient(account(), "secret", smtp_factory=BrokenCleanup).verify()


@pytest.mark.parametrize("recipient,subject,body,category", [
    ("bad-address", "Subject", "Body", "recipient"),
    ("почта@example.ru", "Subject", "Body", "recipient"),
    ("lead@example.ru", "", "Body", "content"),
    ("lead@example.ru", "Subject\nInjected", "Body", "content"),
    ("lead@example.ru", "Subject", " ", "content"),
])
def test_input_failures_are_known_unsent_and_do_not_connect(recipient, subject, body, category):
    FakeSMTP.instances = []
    with pytest.raises(SMTPDeliveryError) as caught:
        MailruSMTPClient(account(), "secret", smtp_factory=FakeSMTP).send(recipient, subject, body)

    assert caught.value.category == category
    assert caught.value.uncertain is False
    assert caught.value.permanent_recipient_failure is False
    assert FakeSMTP.instances == []


@pytest.mark.parametrize("email,display_name", [("invalid-sender", "FuelLead"), ("sender@mail.ru", "FuelLead\nInjected")])
def test_sender_input_failure_is_known_unsent_configuration_error(email, display_name):
    sender_account = SenderAccount(email=email, display_name=display_name)
    FakeSMTP.instances = []
    with pytest.raises(SMTPDeliveryError) as caught:
        MailruSMTPClient(sender_account, "secret", smtp_factory=FakeSMTP).send("lead@example.ru", "Subject", "Body")

    assert caught.value.category == "provider"
    assert caught.value.uncertain is False
    assert FakeSMTP.instances == []


def test_unicode_recipient_domain_uses_idna_in_smtp_envelope():
    FakeSMTP.instances = []
    recipient = "lead@завод.рф"
    encoded = "lead@" + "завод.рф".encode("idna").decode("ascii")
    result = MailruSMTPClient(account(), "secret", smtp_factory=FakeSMTP).send(recipient, "Subject", "Body")

    assert result.smtp_code == "2.0.0"
    assert ("RCPT", encoded) in FakeSMTP.instances[0].commands


def test_reply_headers_and_message_id_survive_smtp_and_sent_copy():
    FakeSMTP.instances = []
    FakeSentIMAP.messages = []
    sender_account = account()
    sender_account.imap_enabled = True
    result = MailruSMTPClient(sender_account, "secret", smtp_factory=FakeSMTP,
        imap_client_factory=FakeSentIMAP, imap_timeout_seconds=17).send(
        "client@example.ru", "Re: Вопрос", "Ответ", message_id="<stable@mail.ru>",
        in_reply_to="<client@example.ru>", references=("<first@mail.ru>", "<client@example.ru>"))
    raw = next(item[1] for item in FakeSMTP.instances[0].commands if isinstance(item, tuple) and item[0] == "DATA")
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    assert msg["Message-ID"] == result.message_id == "<stable@mail.ru>"
    assert msg["In-Reply-To"] == "<client@example.ru>"
    assert msg["References"] == "<first@mail.ru> <client@example.ru>"
    assert not msg["List-Unsubscribe"]
    assert FakeSentIMAP.messages[0][0] == raw


def test_reply_header_injection_is_rejected_before_connect():
    with pytest.raises(SMTPDeliveryError):
        MailruSMTPClient(account(), "secret", smtp_factory=FakeSMTP).send(
            "client@example.ru", "Тема", "Ответ", in_reply_to="<id>\r\nBcc: other@example.ru")
