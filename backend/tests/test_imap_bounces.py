from datetime import datetime, timezone

from app.models import Company, CompanyEmail, EmailSuppression, OutreachCampaign, OutreachDelivery, SenderAccount
from app.services.dsn import parse_permanent_dsn
from app.services.imap_bounces import MailruIMAPClient, apply_dsn_bounce


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
