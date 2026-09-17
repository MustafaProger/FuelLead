from datetime import datetime, timezone
from email.message import EmailMessage

import pytest
from sqlalchemy import select

from app.config import Settings
from app.models import ActivityHistory, Company, CompanyEmail, EmailSuppression, ImapProcessedMessage, OutreachCampaign, OutreachDelivery, SenderAccount
from app.services.imap_bounces import apply_dsn_bounce, process_imap_tick
from app.services.inbound_replies import apply_client_reply, parse_client_reply
from app.services.outreach import OutreachPolicyError, assert_manual_send_allowed


def message(body, *, sender="client@example.ru", subject="Re: Предложение", html=False, reference=None, message_id="<reply@example.ru>"):
    item = EmailMessage()
    item["From"] = sender
    item["To"] = "sender@mail.ru"
    item["Subject"] = subject
    item["Message-ID"] = message_id
    if reference:
        item["In-Reply-To"] = reference
    item.set_content(body, subtype="html" if html else "plain", charset="utf-8")
    return item.as_bytes()


@pytest.mark.parametrize("text", ["не пишите мне больше", "Не писать", "Не интересно", "Нас не интересует", "Неактуально, спасибо", "Удалите нас из рассылки", "Отпишите меня", "Не присылайте больше письма", "Не нужно, спасибо", "unsubscribe", "Do not contact me"])
def test_explicit_refusals(text):
    assert parse_client_reply(message(text)).refusal


@pytest.mark.parametrize("body,html", [
    ("Спасибо! Пришлите условия.\n> Если предложение неактуально, ответьте «Не писать»", False),
    ("Спасибо!\nОт: sender@mail.ru\nНе писать", False),
    ("<p>Пришлите условия</p><blockquote>Не писать</blockquote>", True),
    ("<div>Интересно</div><div class='gmail_quote'><div>не пишите</div></div>", True),
    ("<div>Интересно</div><div class='mail-quote-collapse'><blockquote>Не писать</blockquote></div>", True),
    ("Не пишите цену в рублях, укажите в долларах", False),
])
def test_quotes_and_non_refusals(body, html):
    reply = parse_client_reply(message(body, html=html))
    assert reply and not reply.refusal


def test_subject_only_refusal_with_quoted_offer():
    reply = parse_client_reply(message("> Мы вас приветствуем\n> Если предложение неактуально, ответьте «Не писать»", subject="Не писать"))
    assert reply.refusal == "не писать"


def test_charset_and_html():
    raw = ('From: client@example.ru\r\nContent-Type: text/plain; charset=windows-1251\r\n\r\nНе интересно').encode('cp1251')
    assert parse_client_reply(raw).refusal
    assert parse_client_reply(message('<p>Не&nbsp;пишите мне больше</p>', html=True)).refusal


def test_generated_reports_and_marketing_are_not_client_replies():
    for header in [b'Auto-Submitted: auto-generated', b'Auto-Submitted: custom',
                   b'List-Unsubscribe: <https://example.ru/unsub>', b'List-Id: advertising']:
        assert parse_client_reply(header + b'\r\n' + message('Не интересно')) is None


@pytest.mark.parametrize('header', ['auto-replied', ' Auto-Replied ; owner-email=client@example.ru'])
def test_automatic_reply_is_readable_without_refusal_detection(header):
    raw = ('Auto-Submitted: ' + header + '\r\n').encode() + message('Не пишите в период отпуска')
    reply = parse_client_reply(raw)
    assert reply and reply.is_automatic and reply.refusal is None
    assert reply.text == 'Не пишите в период отпуска'


def test_automatic_list_message_is_still_excluded():
    raw = b'Auto-Submitted: auto-replied\r\nList-Id: advertising\r\n' + message('Автоматическое уведомление')
    assert parse_client_reply(raw) is None


def setup_client(db):
    account = SenderAccount(email="sender@mail.ru", encrypted_password="cipher", imap_enabled=True, imap_uidvalidity=44)
    company = Company(name="Клиент", inn="770000000001", status="sent")
    company.emails = [CompanyEmail(email="client@example.ru"), CompanyEmail(email="second@example.ru")]
    db.add_all([account, company])
    db.commit()
    return account, company


@pytest.mark.parametrize('status', ['new', 'sent', 'answered', 'interested', 'customer', 'rejected'])
def test_automatic_reply_preserves_company_status_and_suppressions(db, status):
    account, company = setup_client(db)
    company.status = status
    db.add(ActivityHistory(company_id=company.id, event_type='email_sent', description='sent', event_data={
        'message_id': '<original@mail.ru>', 'sender_account_id': account.id, 'recipient': 'client@example.ru'}))
    db.commit()
    db.refresh(company)
    updated_at = company.last_updated_at
    raw = b'Auto-Submitted: auto-replied\r\n' + message(
        'Не пишите в период отпуска', sender='assistant@another.ru', reference='<original@mail.ru>')
    assert apply_client_reply(db, account.id, raw)[0] == 'automatic_reply'
    db.commit()
    assert company.status == status and company.last_updated_at == updated_at
    assert db.query(EmailSuppression).count() == 0
    event = db.query(ActivityHistory).filter_by(event_type='email_reply').one()
    assert event.event_data['is_automatic'] is True and event.event_data['refusal'] is None
    assert event.from_status == event.to_status == status


def test_automatic_sender_fallback_requires_outgoing_on_receiving_account(db):
    account, company = setup_client(db)
    raw = b'Auto-Submitted: auto-replied\r\n' + message('Я в отпуске')
    assert apply_client_reply(db, account.id, raw)[0] == 'unmatched_reply'
    outgoing = ActivityHistory(company_id=company.id, event_type='email_sent', description='sent', event_data={
        'message_id': '<original@mail.ru>', 'sender_account_id': 999, 'recipient': 'client@example.ru'})
    db.add(outgoing); db.commit()
    assert apply_client_reply(db, account.id, raw)[0] == 'unmatched_reply'
    outgoing.event_data = {**outgoing.event_data, 'sender_account_id': account.id}
    db.commit()
    assert apply_client_reply(db, account.id, raw)[0] == 'automatic_reply'


def test_automatic_sender_fallback_matches_only_accepted_delivery(db):
    account, company = setup_client(db)
    campaign = OutreachCampaign(daily_limit=50, hourly_limit=0, min_interval_seconds=60, max_per_domain_per_day=0)
    delivery = OutreachDelivery(company_id=company.id, sender_account_id=account.id,
        company_name=company.name, company_inn=company.inn, recipient='client@example.ru', recipient_domain='example.ru',
        subject='Предложение', body='Условия', status='failed')
    campaign.deliveries.append(delivery)
    db.add(campaign); db.commit()
    raw = b'Auto-Submitted: auto-replied\r\n' + message('Я в отпуске')
    assert apply_client_reply(db, account.id, raw)[0] == 'unmatched_reply'
    delivery.status = 'accepted'; db.commit()
    assert apply_client_reply(db, account.id, raw) == ('automatic_reply', delivery.id)


def test_reprocess_discarded_automatic_reply_preserves_cursor_and_read_state(db):
    account, company = setup_client(db)
    account.imap_last_uid = 500
    db.add(ImapProcessedMessage(sender_account_id=account.id, uid=315, outcome='unrecognized'))
    db.add(ActivityHistory(company_id=company.id, event_type='email_sent', description='sent', event_data={
        'message_id': '<original@mail.ru>', 'sender_account_id': account.id, 'recipient': 'client@example.ru'}))
    db.commit()
    raw = b'Auto-Submitted: auto-replied\r\n' + message('Я в отпуске', reference='<original@mail.ru>')
    apply_dsn_bounce(db, account, 315, raw, expected_uidvalidity=44, reprocess=True)
    assert company.status == 'sent' and account.imap_last_uid == 500
    assert db.query(ImapProcessedMessage).one().outcome == 'automatic_reply'
    event = db.query(ActivityHistory).filter_by(event_type='email_reply').one()
    event.event_data = {**event.event_data, 'read_at': '2026-09-17T10:00:00+00:00'}
    db.commit()
    apply_dsn_bounce(db, account, 315, raw, expected_uidvalidity=44, reprocess=True)
    apply_client_reply(db, account.id, raw, mailbox='Archive', uid=5)
    db.commit()
    assert db.query(ActivityHistory).filter_by(event_type='email_reply').count() == 1
    assert event.event_data['read_at'] == '2026-09-17T10:00:00+00:00'
    assert account.imap_last_uid == 500 and db.query(EmailSuppression).count() == 0


def test_refusal_is_atomic_idempotent_and_suppresses_every_company_address(db):
    account, company = setup_client(db)
    raw = message('не пишите мне больше')
    apply_dsn_bounce(db, account, 100, raw, expected_uidvalidity=44)
    apply_dsn_bounce(db, account, 100, raw, expected_uidvalidity=44)
    apply_client_reply(db, account.id, raw, mailbox='Archive', uid=5)
    db.commit()
    assert company.status == 'rejected'
    assert db.query(ActivityHistory).filter_by(event_type='email_reply').count() == 1
    assert {s.email for s in db.scalars(select(EmailSuppression))} == {'client@example.ru', 'second@example.ru'}
    assert db.query(ImapProcessedMessage).one().outcome == 'rejected'
    assert account.imap_last_uid == 100
    for address in ['client@example.ru', 'second@example.ru']:
        with pytest.raises(OutreachPolicyError):
            assert_manual_send_allowed(db, address, Settings(_env_file=None))


def test_backfill_previously_discarded_message_without_rewinding_cursor(db):
    account, company = setup_client(db)
    account.imap_last_uid = 500
    db.add(ImapProcessedMessage(sender_account_id=account.id, uid=90, outcome='unrecognized'))
    db.commit()
    apply_dsn_bounce(db, account, 90, message('Не интересно'), reprocess=True)
    assert company.status == 'rejected'
    assert account.imap_last_uid == 500
    assert db.query(ImapProcessedMessage).one().outcome == 'rejected'


def test_manual_send_reference_matches_reply_from_another_address(db):
    account, company = setup_client(db)
    db.add(ActivityHistory(company_id=company.id, event_type='email_sent', description='sent', event_data={
        'message_id': '<original@mail.ru>', 'sender_account_id': account.id, 'recipient': 'client@example.ru'}))
    db.commit()
    outcome, _ = apply_client_reply(db, account.id, message('Не пишите', sender='director@another.ru', reference='<original@mail.ru>'))
    db.commit()
    assert outcome == 'rejected'
    assert company.status == 'rejected'
    assert db.query(EmailSuppression).filter_by(email='director@another.ru').one()


def test_unknown_sender_and_wrong_account_reference_do_not_change_company(db):
    account, company = setup_client(db)
    db.add(ActivityHistory(company_id=company.id, event_type='email_sent', description='sent', event_data={
        'message_id': '<original@mail.ru>', 'sender_account_id': 999}))
    db.commit()
    assert apply_client_reply(db, account.id, message('Не писать', sender='unknown@example.ru', reference='<original@mail.ru>'))[0] == 'unmatched_reply'
    assert company.status == 'sent'


@pytest.mark.parametrize('status,expected', [('sent','answered'), ('new','answered'), ('interested','interested'), ('customer','customer'), ('rejected','rejected')])
def test_normal_reply_preserves_advanced_status(db, status, expected):
    account, company = setup_client(db)
    company.status = status
    db.commit()
    apply_client_reply(db, account.id, message('Пришлите условия'))
    db.commit()
    assert company.status == expected


def test_reply_effects_rollback_with_receipt_on_error(db, monkeypatch):
    account, company = setup_client(db)
    def fail():
        raise RuntimeError('commit unavailable')
    monkeypatch.setattr(db, 'commit', fail)
    with pytest.raises(RuntimeError):
        apply_dsn_bounce(db, account, 100, message('Не писать'))
    db.rollback()
    assert db.get(Company, company.id).status == 'sent'
    assert db.query(EmailSuppression).count() == 0
    assert db.query(ImapProcessedMessage).count() == 0


def test_folder_cursors_are_independent_and_uidvalidity_replay_is_idempotent(db):
    from sqlalchemy.orm import sessionmaker
    from app.models import ImapReplyFolder
    from app.services.credentials import CredentialCipher, generate_encryption_key
    from app.services.imap_bounces import process_reply_folders_tick
    account, company = setup_client(db)
    settings = Settings(_env_file=None, mail_credentials_encryption_key=generate_encryption_key())
    account.encrypted_password = CredentialCipher(settings.mail_credentials_encryption_key).encrypt('test')
    account.imap_last_uid = 500
    db.commit()
    class Folders:
        validity = 1
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def reply_mailboxes(self): return ['Spam', 'Archive']
        def messages_after(self, last_uid, limit, *, mailbox, expected_uidvalidity):
            self.uidvalidity = self.validity
            self.uid_namespace_reset = expected_uidvalidity not in (None, self.validity)
            if self.uid_namespace_reset: last_uid = 0
            return [(1, message('Не писать'))] if last_uid < 1 else []
    for version in [1, 1, 2]:
        Folders.validity = version
        process_reply_folders_tick(settings, session_factory=sessionmaker(bind=db.get_bind()), client_factory=Folders)
    db.expire_all()
    assert company.status == 'rejected'
    assert account.imap_last_uid == 500
    assert account.imap_uidvalidity == 44
    assert db.query(ActivityHistory).filter_by(event_type='email_reply').count() == 1
    assert {(s.mailbox, s.uidvalidity, s.last_uid) for s in db.scalars(select(ImapReplyFolder))} == {('Spam', 2, 1), ('Archive', 2, 1)}


def test_actual_worker_applies_reply_without_external_manual_status_change(db):
    from sqlalchemy.orm import sessionmaker
    from app.services.credentials import CredentialCipher, generate_encryption_key
    account, company = setup_client(db)
    settings = Settings(_env_file=None, mail_credentials_encryption_key=generate_encryption_key())
    account.encrypted_password = CredentialCipher(settings.mail_credentials_encryption_key).encrypt('test')
    db.commit()
    class Inbox:
        uidvalidity = 44
        uid_namespace_reset = False
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def messages_after(self, *args): return [(90, message('не пишите мне больше'))]
    process_imap_tick(settings, session_factory=sessionmaker(bind=db.get_bind()), client_factory=Inbox)
    db.expire_all()
    assert company.status == 'rejected'
    assert account.imap_last_uid == 90


def test_manual_status_rejection_blocks_manual_send(db):
    from fastapi import HTTPException
    from app.main import send_company_email, update_company_status
    from app.schemas import EmailSendRequest, StatusUpdate
    account, company = setup_client(db)
    settings = Settings(_env_file=None)
    update_company_status(company.id, StatusUpdate(status='rejected'), db, settings)
    assert db.query(EmailSuppression).count() == 2
    with pytest.raises(HTTPException) as caught:
        send_company_email(company.id, EmailSendRequest(recipient='client@example.ru'), db, settings)
    assert caught.value.status_code == 409


def test_late_manual_smtp_success_cannot_overwrite_imap_refusal(db, monkeypatch):
    from app import main
    from app.schemas import EmailSendRequest
    from app.services.credentials import CredentialCipher, generate_encryption_key
    from app.services.smtp import SMTPAccepted
    account, company = setup_client(db)
    settings = Settings(_env_file=None, mail_credentials_encryption_key=generate_encryption_key())
    account.encrypted_password = CredentialCipher(settings.mail_credentials_encryption_key).encrypt('test')
    account.verification_status = 'verified'
    db.commit()
    class Transport:
        def __init__(self, *args, **kwargs): pass
        def send(self, *args, **kwargs):
            # IMAP commits while the SMTP call is in flight.
            apply_client_reply(db, account.id, message('Не писать'))
            db.commit()
            return SMTPAccepted('<outbound@mail.ru>', '250', 'OK')
    monkeypatch.setattr(main, 'MailruSMTPClient', Transport)
    main.send_company_email(company.id, EmailSendRequest(recipient='client@example.ru'), db, settings)
    db.refresh(company)
    assert company.status == 'rejected'
    assert db.query(EmailSuppression).count() == 2


def test_snapshot_recipient_is_suppressed_after_refusal(db):
    from app.services.outreach import _pre_send_suppression_reason
    from app.models import OutreachDelivery
    account, company = setup_client(db)
    company.status = 'new'
    db.commit()
    delivery = OutreachDelivery(company_id=company.id, recipient='client@example.ru')
    apply_client_reply(db, account.id, message('Не интересно'))
    db.commit()
    assert _pre_send_suppression_reason(db, delivery)
